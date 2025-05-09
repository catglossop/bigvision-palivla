import os 
import numpy as np
import yaml
import json
import itertools
import argparse
import random
import glob
import cv2
policys = {
            "cf_filtered" : ("cat-logs/cf_filtered_data_fixed_2025_04_21_15_51_43", 145000), # DONE 
            # "cf_filtered_w_atomic" : ("cat-logs/cf_filtered_w_atomic_2025_04_23_00_17_54", 140000), # DONE
            # "orig_only" : ("cat-logs/orig_only_skip_norm_2025_04_01_23_49_15", 145000),
            # "filtered_only": ("cat-logs/filtered_only_2025_04_18_21_15_50", 110000), # DONE
            # "cf_only": ("cat-logs/cf_only_2025_04_22_22_02_52", 125000), # DONE
           }
NUM_TRIALS = 3

os.environ["CUDA_VISIBLE_DEVICES"] = "0"

def check_done_evals(env_name):
    done_evals = glob.glob(f"eval_results/{env_name}/*.json")
    done_evals = [json.load(open(eval_file, "r")) for eval_file in done_evals]
    done_eval_infos = [(eval_info["prompt"], eval_info["policy_type"], eval_info["trial_num"]) for eval_info in done_evals]

    return done_eval_infos

def main(args):
    
    # Load env info 
    with open(args.env_info_path, "r") as f:
        env_info = yaml.safe_load(f)
    
    prompts = env_info["prompts"] 
    env_name = env_info["env_name"]   

    # Set up the eval file
    os.makedirs("eval_results", exist_ok=True)
    os.makedirs(f"eval_results/{env_name}", exist_ok=True)

    # All possible eval combos for this env
    eval_combos = list(itertools.product(prompts, list(policys.keys())))
    eval_combos = [(eval_combo[0], eval_combo[1], trial_num) for eval_combo in eval_combos for trial_num in range(NUM_TRIALS)]
    random.shuffle(eval_combos)
    print("Total evals:", len(eval_combos))

    # Check if we have already done some of these evals
    done_evals = check_done_evals(env_name)
    eval_combos = [eval_combo for eval_combo in eval_combos if eval_combo not in done_evals]
    print("Evals to run:", len(eval_combos))

    curr_trial = len(done_evals)

    # Launch the inference server
    stop = False
    while not stop:
        eval_trial_info = eval_combos.pop()
        prompt = eval_trial_info[0]
        policy = policys[eval_trial_info[1]]
        trial_num = eval_trial_info[2]
        print("Running eval:", eval_trial_info)
        try:
            os.system(f"python ~/bigvision-palivla/scripts/inference_server.py\
                --config ~/bigvision-palivla/configs/nav_config_inference.py\
                --resume_checkpoint_dir {policy[0]}\
                --resume_checkpoint_step {policy[1]}\
                --prompt {prompt}")
        except KeyboardInterrupt:
            print("Finished trial")
            pass

        # Give the eval 

        prompts_print = "\n".join(prompts[:-1])
        prompt_eval = input(f"Which prompt was the policy following?\n{prompts_print}\n")

        # Save the eval info
        eval_results = {
            "prompt": prompt,
            "policy": policy,
            "trial_num": trial_num,
            "prompt_eval": prompt_eval,
            "policy_type": eval_trial_info[1]
        }

        with open(f"eval_results/{env_name}/trial_{curr_trial}.json", "w") as f:
            json.dump(eval_results, f)
        curr_trial += 1
        stop_input = input("Stop? (y/n)")
        if stop_input == "y":
            stop = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env_info_path", type=str, default="env_configs/env_test.yaml")
    args = parser.parse_args()
    main(args)


    


    