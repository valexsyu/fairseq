CUDA_VISIBLE_DEVICES=0,1,2 bash call_scripts/train_nat.sh -e 2-2-1-1-H12-UF50T --twcc --fp16 --save-interval-updates 70000 --max-update 100000 --max-tokens 3072 -b 12288 -g 4 --dropout 0.1
CUDA_VISIBLE_DEVICES=0,1,2 bash call_scripts/generate_nat.sh -e 2-2-1-1-H12-UF50T --twcc -b 50 --data-subset test-valid --no-atten-mask 
CUDA_VISIBLE_DEVICES=0,1,2 bash call_scripts/generate_nat.sh -e 2-2-1-1-H12-UF50T --twcc -b 80 --data-subset test-valid --avg-ck-turnoff