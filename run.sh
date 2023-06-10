# train on FSC147
python train.py --tag CHSNet-qnrf --no-wandb --device 0 --max-noisy-ratio 0.05 --max-weight-ratio 0.5 --scheduler cosine --dcsize 4 --lr 4e-5 --data-dir ../DATASET/FSC147-train-test-dmapfix15 --val-start 200 --val-epoch 5


