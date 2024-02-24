# Borrowed from ip-adapter resampler.py.
# https://github.com/tencent-ailab/IP-Adapter/blob/main/ip_adapter/resampler.py
# modified from https://github.com/mlfoundations/open_flamingo/blob/main/open_flamingo/src/helpers.py
# and https://github.com/lucidrains/imagen-pytorch/blob/main/imagen_pytorch/imagen_pytorch.py

import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from einops.layers.torch import Rearrange
from transformers import CLIPVisionModel
import numpy as np
from torch import einsum
from dataclasses import dataclass
from typing import Optional, Tuple
from transformers.utils import ModelOutput

def reshape_tensor(x, heads):
    bs, length, width = x.shape
    # (bs, length, width) --> (bs, length, n_heads, dim_per_head)
    x = x.view(bs, length, heads, -1)
    # (bs, length, n_heads, dim_per_head) --> (bs, n_heads, length, dim_per_head)
    x = x.transpose(1, 2)
    # (bs, n_heads, length, dim_per_head) --> (bs*n_heads, length, dim_per_head)
    x = x.reshape(bs, heads, length, -1)
    return x


def masked_mean(t, *, dim, mask=None):
    if mask is None:
        return t.mean(dim=dim)

    denom = mask.sum(dim=dim, keepdim=True)
    mask = rearrange(mask, "b n -> b n 1")
    masked_t = t.masked_fill(~mask, 0.0)

    return masked_t.sum(dim=dim) / denom.clamp(min=1e-5)


# FFN
def FeedForward(dim, mult=4):
    inner_dim = int(dim * mult)
    return nn.Sequential(
        nn.Linear(dim, inner_dim, bias=False),
        nn.LayerNorm(inner_dim, elementwise_affine=True),
        nn.Dropout(0.1),
        nn.GELU(),
        nn.Linear(inner_dim, dim, bias=False),
        nn.LayerNorm(dim, elementwise_affine=True),
        nn.Dropout(0.1),
    )

def LoRA_Emb2Queries(input_dim, lora_rank, output_dim, num_output_queries):
    return nn.Sequential(
        # Project to [BS, lora_rank * output_dim].
        nn.Linear(input_dim, lora_rank * output_dim, bias=False),
        # Reshape to [BS, lora_rank, output_dim].
        Rearrange('b (q d) -> b q d', q=lora_rank, d=output_dim),
        nn.LayerNorm(output_dim, elementwise_affine=True),
        nn.Dropout(0.1),
        # Permute to [BS, output_dim, lora_rank].
        Rearrange('b q d -> b d q'),
        # Project to [BS, output_dim, num_output_queries].
        nn.Linear(lora_rank, num_output_queries, bias=False),
        # Permute to [BS, num_output_queries, output_dim].
        Rearrange('b d q -> b q d'),
    )

class PerceiverAttention(nn.Module):
    def __init__(self, *, dim, dim_head=64, heads=8, elementwise_affine=True):
        super().__init__()
        self.scale = dim_head**-0.5
        self.dim_head = dim_head
        self.heads = heads
        inner_dim = dim_head * heads

        self.norm1 = nn.LayerNorm(dim, elementwise_affine=elementwise_affine)
        self.norm2 = nn.LayerNorm(dim, elementwise_affine=elementwise_affine)

        self.to_q   = nn.Linear(dim, inner_dim, bias=False)
        self.to_kv  = nn.Linear(dim, inner_dim * 2, bias=False)
        self.to_out = nn.Linear(inner_dim, dim, bias=False)

    def forward(self, x, latent_queries):
        """
        Args:
            x (torch.Tensor): image features
                shape (b, n1, D)
            latent (torch.Tensor): latent features
                shape (b, n2, D)
        """
        x = self.norm1(x)
        latent_queries = self.norm2(latent_queries)

        b, l, _ = latent_queries.shape

        q = self.to_q(latent_queries)
        kv_input = torch.cat((x, latent_queries), dim=-2)
        k, v = self.to_kv(kv_input).chunk(2, dim=-1)
        
        q = reshape_tensor(q, self.heads)
        k = reshape_tensor(k, self.heads)
        v = reshape_tensor(v, self.heads)

        # attention
        scale = 1 / math.sqrt(math.sqrt(self.dim_head))
        weight = (q * scale) @ (k * scale).transpose(-2, -1)  # More stable with f16 than dividing afterwards
        attn = torch.softmax(weight.float(), dim=-1).type(weight.dtype)
        out = attn @ v

        out = out.permute(0, 2, 1, 3).reshape(b, l, -1)

        return self.to_out(out)


# All CrossAttention layers have 8 heads.
class CrossAttention(nn.Module):
    def __init__(self, input_dim, heads=8, dropout=0.2):
        super().__init__()
        dim_head = input_dim // heads
        inner_dim = dim_head * heads

        self.scale = dim_head ** -0.25
        self.heads = heads

        # To increase stability,, we add layernorm to q,k,v projections.
        self.to_q = nn.Sequential(
                        nn.Linear(input_dim, inner_dim, bias=False),
                        nn.LayerNorm(inner_dim, elementwise_affine=True))
        
        self.to_k = nn.Sequential(
                        nn.Linear(input_dim, inner_dim, bias=False),
                        nn.LayerNorm(inner_dim, elementwise_affine=True))
        
        self.to_v = nn.Sequential(
                        nn.Linear(input_dim, inner_dim, bias=False),
                        nn.LayerNorm(inner_dim, elementwise_affine=True))

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, input_dim, bias=False),
            nn.LayerNorm(inner_dim, elementwise_affine=True),
            nn.Dropout(dropout)
        )
        
    def forward(self, x, context=None):
        h = self.heads

        q = self.to_q(x)
        if context is None:
            context = x

        k = self.to_k(context)            
        v = self.to_v(context)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (q, k, v))
        # * 5 to increase the polarity of the attention scores. Otherwise the variance is too small
        # and the attention is too uniform.
        '''
        for N in (768, 7680, 76800, 768000):
            ln = nn.LayerNorm(N)
            a = torch.randn(100, N)
            b = torch.randn(100, N)
            x=(ln(a) * ln(b)).sum(dim=1) * 5 / (N**0.5)
            print(x.std(dim=0))
            
        tensor(4.6600, grad_fn=<StdBackward0>)
        tensor(5.3220, grad_fn=<StdBackward0>)
        tensor(4.8963, grad_fn=<StdBackward0>)
        tensor(5.0100, grad_fn=<StdBackward0>)        
        '''        
        scale = q.size(-1) ** -0.5
        sim = einsum('b i d, b j d -> b i j', q * scale, k * scale) * 5
        # sim: [16, 378, 257]. 16: bs 1 * h 16.
        # attention, what we cannot get enough of
        # NOTE: the normalization is done across tokens, not across pixels.
        # So for each pixel, the sum of attention scores across tokens is 1.
        attn = sim.softmax(dim=-1)

        # v: [16, 257, 48]. 48: dim of each head. out: [16, 378, 48].
        out = einsum('b i j, b j d -> b i d', attn, v)
        # [16, 378, 48] -> [1, 378, 768].
        out = rearrange(out, '(b h) n d -> b n (h d)', h=h)

        out = self.to_out(out)

        return out

# Simplified from IP-Adapter Resampler.
class SubjBasisGenerator(nn.Module):
    def __init__(
        self,
        dim=768,                            # Internal feature dimension. Same as output_dim.
        depth=2,                            # number of (CrossAttention, FeedForward) layers.     
        # number of heads as per https://huggingface.co/laion/CLIP-ViT-H-14-laion2B-s32B-b79K/blob/main/config.json        
        heads=16,       
        # num_subj_queries: number of subject latent_queries.
        # num_bg_queries:   number of background latent_queries.
        # 2 * Static/Ada layerwise_lora_rank. * 2 to generate both static and ada bases.
        # The same SubjBasisGenerator instance is used to generate both subj and bg embedder bases, 
        # provided different input features in two different passes.
        num_subj_queries=378,               # 378 = 9 * 42.
        num_bg_queries=168,                 # 168 = 4 * 42.
        image_embedding_dim=1280,           # CLIP image feature dimension, as per config.json above.
        face_embedding_dim=512,             # insightface face feature dimension for humans.
        dino_embedding_dim=384,             # DINO object feature dimension for objects.
        num_lora_queries=32,                # number of low-rank latent_queries.
        output_dim=768,                     # CLIP text embedding input dimension.
        ff_mult=1,                          # FF inner_dim = dim * ff_mult. Set to 1 to reduce the number of parameters.
        max_seq_len: int = 257,             # [CLS token, image tokens]
        apply_pos_emb: bool = True,         # Newer IP Adapter uses positional embeddings.
        use_id_embs: bool   = True,         # Whether to use an identity embedding to generate latent_queries.
    ):
        super().__init__()
        self.proj_in = nn.Sequential(
            nn.Linear(image_embedding_dim, dim, bias=False),
            nn.LayerNorm(dim, elementwise_affine=True),
        )
        self.face_proj_in = LoRA_Emb2Queries(face_embedding_dim, num_lora_queries, dim, num_subj_queries)
        self.obj_proj_in  = LoRA_Emb2Queries(dino_embedding_dim, num_lora_queries, dim, num_subj_queries)

        self.pos_emb    = nn.Embedding(max_seq_len, dim)            if apply_pos_emb else None
        self.pos_emb_ln = nn.LayerNorm(dim, elementwise_affine=True)   if apply_pos_emb else None

        self.latent_subj_queries = nn.Parameter(torch.randn(1, num_subj_queries, dim) / dim**0.5)
        self.latent_bg_queries   = nn.Parameter(torch.randn(1, num_bg_queries, dim)   / dim**0.5)
        self.lq_ln               = nn.LayerNorm(dim, elementwise_affine=True)

        # Remove proj_out to reduce the number of parameters, since image_embedding_dim = output_dim = 768.
        self.proj_out = nn.Identity() #nn.Linear(dim, output_dim)
        self.norm_out = nn.LayerNorm(output_dim, elementwise_affine=True)
        self.output_scale = output_dim ** -0.5

        self.depth = depth
        self.num_subj_queries = num_subj_queries
        self.num_bg_queries = num_bg_queries

        if not use_id_embs:
            assert depth > 0, "depth must be > 0 if not use_id_embs."

        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(
                nn.ModuleList(
                    [
                        # dim=768, heads=16.
                        CrossAttention(input_dim=dim, heads=heads, dropout=0.2),
                        # FeedForward: 2-layer MLP with GELU activation.
                        # LayerNorm -> Linear -> GELU -> Linear.
                        FeedForward(dim=dim, mult=ff_mult),
                    ]
                )
            )
        self.use_id_embs = use_id_embs

        print(repr(self))

    def forward(self, clip_features, id_embs, is_face, placeholder_is_bg=False):     
        x = self.proj_in(clip_features)

        # No need to use id_embs if placeholder_is_bg, or if face embs are disabled (use_id_embs is False), 
        # or no face is detected (id_embs is all 0s).
        if self.use_id_embs and (id_embs is not None) and (id_embs != 0).any() and (not placeholder_is_bg):
            # id_embs: [1, 512].
            # latent_subj_queries: [1, 378, 768].
            if is_face:
                latent_subj_queries = self.face_proj_in(id_embs)
            else:
                latent_subj_queries = self.obj_proj_in(id_embs)
        else:
            latent_subj_queries = self.latent_subj_queries

        # placeholder_is_bg determines whether to select latent_bg_queries or latent_subj_queries,
        # which then extract either background or subject bases.
        latent_queries = self.latent_bg_queries if placeholder_is_bg else latent_subj_queries        
        latent_queries = self.lq_ln(latent_queries.repeat(x.size(0), 1, 1))

        if self.pos_emb is not None:
            n, device = x.shape[1], x.device
            pos_emb = self.pos_emb(torch.arange(n, device=device))
            # Downscale positional embeddings to reduce its impact.
            x = x + self.pos_emb_ln(pos_emb) * 0.5

        # If use_id_embs and depth = 0, then latent_queries from id_embs is directly returned.
        # If not use_id_embs, then depth must be > 0.
        for i, (attn, ff) in enumerate(self.layers):
            if not placeholder_is_bg:
                latent_queries = attn(latent_queries, x) + latent_queries
            else:
                latent_queries = attn(latent_queries, x)

            latent_queries = ff(latent_queries) + latent_queries

        latent_queries = self.proj_out(latent_queries)
        return self.norm_out(latent_queries) * self.output_scale

    def __repr__(self):
        if not hasattr(self, 'use_id_embs'):
            self.use_id_embs = True
        if not hasattr(self, 'depth'):
            self.depth = len(self.layers)
        if not hasattr(self, 'num_subj_queries'):
            self.num_subj_queries = self.latent_subj_queries.shape[1]
        if not hasattr(self, 'num_bg_queries'):
            self.num_bg_queries = self.latent_bg_queries.shape[1]

        return f"SubjBasisGenerator: depth={self.depth}, num_subj_queries={self.num_subj_queries}, num_bg_queries={self.num_bg_queries}, use_id_embs={self.use_id_embs}"
    
@dataclass
class BaseModelOutputWithPooling2(ModelOutput):
    """
    Base class for model's outputs that also contains a pooling of the last hidden states.

    Args:
        last_hidden_state (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.
        pooler_output (`torch.FloatTensor` of shape `(batch_size, hidden_size)`):
            Last layer hidden-state of the first token of the sequence (classification token) after further processing
            through the layers used for the auxiliary pretraining task. E.g. for BERT-family of models, this returns
            the classification token after processing through a linear layer and a tanh activation function. The linear
            layer weights are trained from the next sentence prediction (classification) objective during pretraining.
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
    """

    last_hidden_state: torch.FloatTensor = None
    pooler_output: torch.FloatTensor = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    attn_mask: Optional[torch.FloatTensor] = None

# Revised from CLIPVisionTransformer. self: a CLIPVisionTransformer instance.
# https://github.com/huggingface/transformers/blob/main/src/transformers/models/clip/modeling_clip.py#L821
# pixel_values: preprocessed B*C*H*W images. [BS, 3, 224, 224]
# attn_mask: B*H*W attention mask.
def CLIPVisionTransformer_forward(self, pixel_values = None, attn_mask=None, 
                                  output_attentions = None,
                                  output_hidden_states = None, return_dict = None):

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if pixel_values is None:
            raise ValueError("You have to specify pixel_values")

        # Visual tokens are flattended in embeddings().
        # self.embeddings: CLIPVisionEmbeddings.
        # hidden_states: [BS, 257, 1280]. 257: 16*16 (patch_embeds) + 1 (class_embeds).
        # 16*16 is output from Conv2d(3, 1280, kernel_size=(14, 14), stride=(14, 14), bias=False).
        hidden_states = self.embeddings(pixel_values)
        hidden_states = self.pre_layrnorm(hidden_states)
        
        if attn_mask is not None:
            # feat_edge_size: 16.
            feat_edge_size = np.sqrt(hidden_states.shape[1] - 1).astype(int)
            # attn_mask: [BS, 512, 512] -> [BS, 1, 16, 16].
            attn_mask = F.interpolate(attn_mask.unsqueeze(1), size=(feat_edge_size, feat_edge_size), mode='nearest')
            # Flatten the mask: [BS, 1, 16, 16] => [BS, 1, 256].
            attn_mask = attn_mask.flatten(2)
            # Prepend 1 to the mask: [BS, 1, 256] => [BS, 1, 257]. 
            # This 1 corresponds to class_embeds, which is always attended to.
            attn_mask = torch.cat([torch.ones(*attn_mask.shape[:2], 1).to(attn_mask.device), attn_mask], dim=-1)
            attn_mask_pairs = torch.matmul(attn_mask.transpose(-1, -2), attn_mask).unsqueeze(1)
        else:
            attn_mask_pairs = None

        # encoder: CLIPEncoder.
        encoder_outputs = self.encoder(
            inputs_embeds=hidden_states,
            # New feature: (***The official documentation is wrong***)
            # attention_mask (`torch.Tensor` of shape `(batch_size, 1, sequence_length, sequence_length)`, *optional*):
            #                 Mask to avoid performing attention on pairs of token. Mask values selected in `[0, 1]`:
            #                 - 1 for pairs that are **not masked**,
            #                 - 0 for pairs that are **masked**.    
            # attention_mask is eventually used by CLIPEncoderLayer:
            # https://github.com/huggingface/transformers/blob/main/src/transformers/models/clip/modeling_clip.py#L370
            attention_mask=attn_mask_pairs,
            output_attentions=output_attentions,        # False
            output_hidden_states=output_hidden_states,  # True
            return_dict=return_dict,                    # True
        )

        # last_hidden_state: [BS, 257, 1280]
        last_hidden_state = encoder_outputs[0]
        pooled_output = last_hidden_state[:, 0, :]
        pooled_output = self.post_layernorm(pooled_output)

        # return_dict is True.
        if not return_dict:
            return (last_hidden_state, pooled_output) + encoder_outputs[1:]

        return BaseModelOutputWithPooling2(
            last_hidden_state=last_hidden_state,
            pooler_output=pooled_output,
            hidden_states=encoder_outputs.hidden_states,
            attentions=encoder_outputs.attentions,
            # Newly added: return resized flattened attention mask.
            # [BS, 1, 257] -> [BS, 257, 1]
            attn_mask=attn_mask.permute(0, 2, 1) if attn_mask is not None else None
        )

class CLIPVisionModelWithMask(CLIPVisionModel):
    def __init__(self, config):
        super().__init__(config)
        # Replace vision_model.forward() with the new one that supports mask.
        self.vision_model.forward = CLIPVisionTransformer_forward.__get__(self.vision_model)
    
    def forward(self, pixel_values = None, attn_mask = None, output_attentions = None,
                output_hidden_states = None, return_dict = None):
        
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        return self.vision_model(
            pixel_values=pixel_values,
            attn_mask=attn_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        ) 
    
