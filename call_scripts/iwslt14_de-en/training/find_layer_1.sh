source $HOME/.bashrc 
conda activate base

bash call_scripts/iwslt14_de-en/training/train_nat.sh -e 1-1-1-1-H11-UF20M 
bash call_scripts/iwslt14_de-en/training/train_nat.sh -e 2-2-1-1-H12-UR45M