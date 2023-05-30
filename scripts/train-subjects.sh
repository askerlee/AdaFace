#!/usr/bin/fish
# Trainign scripts for the 25 subjects, using AdaPrompt/TI/DreamBooth.
set self (status basename)
echo $self $argv

argparse --ignore-unknown --min-args 1 --max-args 20 'gpu=' 'maxiter=' 'lr=' 'subjfile=' 'selset' 'skipselset' 'cls_token_as_delta' 'cls_token_as_distill' 'use_z_suffix' 'eval' 'ema' 'v14' -- $argv
or begin
    echo "Usage: $self [--gpu ID] [--maxiter M] [--lr LR] [--subjfile SUBJ] [--cls_token_as_delta] [--cls_token_as_distill] [--use_z_suffix] [--eval] (ada|ti|db) [--selset|low high] [EXTRA_ARGS]"
    echo "E.g.:  $self --gpu 0 --maxiter 4000 --subjfile evaluation/info-dbeval-subjects.sh --cls_token_as_delta ada 1 25"
    exit 1
end

if [ "$argv[1]" = 'ada' ];  or [ "$argv[1]" = 'static-layerwise' ]; or [ "$argv[1]" = 'ti' ]; or [ "$argv[1]" = 'db' ]
    set method $argv[1]
else
    echo "Usage: $self [--gpu ID] [--maxiter M] [--lr LR] [--subjfile SUBJ] [--cls_token_as_delta] [--cls_token_as_distill] [--use_z_suffix] [--eval] (ada|ti|db) [--selset|low high] [EXTRA_ARGS]"
    echo "E.g.:  $self --gpu 0 --maxiter 4000 --subjfile evaluation/info-dbeval-subjects.sh --cls_token_as_delta ada 1 25"
    exit 1
end

set -q _flag_subjfile; and set subj_file $_flag_subjfile; or set subj_file evaluation/info-subjects.sh
if ! test -e $subj_file
    echo "Error: Subject file '$subj_file' does not exist."
    exit 1
end
source $subj_file

set -q _flag_gpu; and set GPU $_flag_gpu; or set GPU 0
# BUGGY: if L, H are not specified, then $argv[2], $argv[3] may contain unrecognized arguments.
set -q argv[2]; and set L $argv[2]; or set L 1
set -q argv[3]; and set H $argv[3]; or set H (count $subjects)
set EXTRA_ARGS0 $argv[4..-1]

set -q _flag_lr; and set lr $_flag_lr; or set -e lr
set -q _flag_min_rand_scaling; and set min_rand_scaling $_flag_min_rand_scaling; or set -e min_rand_scaling
#set fish_trace 1

if set -q _flag_v14
    set sd_ckpt models/stable-diffusion-v-1-4-original/sd-v1-4.ckpt
else if set -q _flag_ema
    set sd_ckpt models/stable-diffusion-v-1-5/v1-5-pruned-emaonly.ckpt
else
    set sd_ckpt models/stable-diffusion-v-1-5/v1-5-pruned.ckpt
end

# If --selset is given, then only train on the selected subjects, specified in $subj_file.
set -q _flag_selset; and set -l indices0 $sel_set; or set -l indices0 (seq 1 (count $subjects))
set -l indices $indices0[(seq $L $H)]

echo Training on $subjects[$indices]

# $0 0 1 13: alexachung .. masatosakai, on GPU0
# $0 1 14 25: michelleyeoh .. zendaya,  on GPU1
for i in $indices
    if set -q _flag_skipselset; and contains $i $sel_set
        echo "Skipping $i: $subjects[$i]"
        continue
    end

    set subject     $subjects[$i]
    set ada_prompt  $ada_prompts[$i]
    set ada_weight  (string split " " $ada_weights[$i])
    # If cls_tokens is specified in subjfile, cls_token = cls_tokens[$i]. 
    # Otherwise, cls_token is the last word of ada_prompt. 
    # For non-human cases "stuffed animal", the last word of ada_prompt is "animal", which is incorrecct. 
    # So we need an individual cls_tokens for non-human subjects. 
    # For "stuffed animal", the corresponding cls_token is "toy".
    # For humans, this is optional. If not specified, then cls_token = last word of ada_prompt.
    # Only use cls_token as delta token when --cls_token_as_delta is specified.
    set -q cls_tokens; and set cls_token $cls_tokens[$i]; or set cls_token (string split " " $ada_prompt)[-1]
    set db_prompt0 "$db_prompts[$i]"
    set db_prompt  "$db_prompt0$db_suffix"

    if [ $method = 'ti' ]; or [ $method = 'ada' ]; or [ $method = 'static-layerwise' ]
        if [ $method = 'ada' ]; or [ $method = 'static-layerwise' ]
            set initword $ada_prompt
            set init_word_weights $ada_weight
        else
            set initword $cls_token
            set init_word_weights 1
        end

        # If $broad_classes are specified in subjfile, then use it. Otherwise, use the default value 1.
        set -q broad_classes; and set broad_class $broad_classes[$i]; or set broad_class 1

        if not set -q _flag_maxiter
            # -1: use the default max_iters.
            set -q maxiters; and set max_iters $maxiters[(math $broad_class+1)]; or set max_iters -1
        else
            # Use the specified max_iters.
            set max_iters $_flag_maxiter
        end

        # Reset EXTRA_ARGS1 to EXTRA_ARGS0 each time. 
        set EXTRA_ARGS1 $EXTRA_ARGS0

        # cls_token: the class token used in delta loss computation.
        # If --cls_token_as_delta, and cls_tokens is provided in the subjfile, then use cls_token. 
        # Otherwise use the default cls_token "person".
        set -q _flag_cls_token_as_delta; and set EXTRA_ARGS1 $EXTRA_ARGS1 --cls_delta_token $cls_token
        set -q _flag_cls_token_as_distill; and set EXTRA_ARGS1 $EXTRA_ARGS1 --cls_distill_token $cls_token
        
        # z_suffix: append $cls_token as a suffix to "z" in the prompt. The prompt will be "a z <cls_token> <prompt>".
        # E.g., cls_token="toy", prompt="in a chair", then full prompt="a z toy in a chair".
        # If not specified, then no suffix is appended. The prompt will be "a z <prompt>". E.g. "a z in a chair".
        set -q _flag_use_z_suffix;  and set z_suffix $cls_token; or set -e z_suffix
        set -q z_suffix; and set EXTRA_ARGS1 $EXTRA_ARGS1 --placeholder_suffix $z_suffix

        if not set -q _flag_lr
            set -q lrs; and set lr $lrs[(math $broad_class+1)]
        end
        set -q lr; and set EXTRA_ARGS1 $EXTRA_ARGS1 --lr $lr
        set -q min_rand_scaling; and set EXTRA_ARGS1 $EXTRA_ARGS1 --min_rand_scaling $min_rand_scaling

        echo $subject: --init_word $initword $EXTRA_ARGS1
        set fish_trace 1
        python3 main.py --base configs/stable-diffusion/v1-finetune-$method.yaml  -t --actual_resume $sd_ckpt --gpus $GPU, --data_root $data_folder/$subject/ -n $subject-$method --no-test --max_steps $max_iters --placeholder_string "z" --init_word $initword --init_word_weights $init_word_weights --broad_class $broad_class $EXTRA_ARGS1

        if set -q _flag_eval
            if [ "$data_folder"  = 'dbeval-dataset' ]
                set out_dir_tmpl 'samples-dbeval'
            elif [ "$data_folder" = 'ti-dataset' ]
                set out_dir_tmpl 'samples-tieval'
            else
                set out_dir_tmpl 'samples'
            end
            python3 scripts/gen_subjects_and_eval.py --method $method --scale 10 --gpu $GPU --subjfile $subj_file --out_dir_tmpl $out_dir_tmpl  --compare_with_pardir $data_folder --range $i
        end

    else
        echo $subject: $db_prompt

        # -1: use the default max_iters.
        set -q _flag_maxiter; and set max_iters $_flag_maxiter; or set max_iters -1        
        set fish_trace 1
        # $EXTRA_ARGS is not for DreamBooth. It is for AdaPrompt/TI only.
        python3 main.py --base configs/stable-diffusion/v1-finetune_unfrozen.yaml -t --actual_resume $sd_ckpt --gpus $GPU, --reg_data_root regularization_images/(string replace -a " " "" $db_prompt0) --data_root $data_folder/$subject -n $subject-dreambooth --no-test --max_steps $max_iters --lr $lr --token "z" --class_word $db_prompt
    end

    set -e fish_trace
end
