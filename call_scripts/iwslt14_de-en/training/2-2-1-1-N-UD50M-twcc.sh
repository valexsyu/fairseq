CUDA_VISIBLE_DEVICES=0 bash call_scripts/iwslt14_de-en/training/train_nat.sh -e 2-2-1-1-N-UD50M --fp16 --save-interval-updates 70000 --twcc --max-tokens 2048