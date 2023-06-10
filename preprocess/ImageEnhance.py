from PIL import ImageEnhance
from PIL import Image
import os
import re
import argparse
import json
from tqdm import tqdm

class ImageEnhance(data_path):
    anno_file = data_path + r'\annotation_FSC147_384.json'
    data_split_file = data_path + r'\Train_Test_Val_FSC_147.json'
    im_dir = data_path + r'\images_384_VarV2'

    with open(anno_file) as f:
        annotations = json.load(f)

    with open(data_split_file) as f:
        data_split = json.load(f)

    im_ids = data_split[args.test_split]
    pbar = tqdm(im_ids)

    for im_id in pbar:
        img = Image.open('{}/{}'.format(im_dir, im_id))
        #print(Image)
        enh_con = ImageEnhance.Contrast(img)
        contrast = 2
        img_pos = enh_con.enhance(contrast)
        # img_pos.show()
        # img_contrasted.save("./709_new.jpg")

        img_pos.save(  # remove the '.jpg' suffix from an image file's path
            '{}/{}.jpg'.format(im_dir, im_id.strip('.jpg')))
        #print('image {} done!'.format(im_id.strip('.jpg')))
        img.close()
