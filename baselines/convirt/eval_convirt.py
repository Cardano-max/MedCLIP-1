import pdb, os
import math
import random
import re
from collections import defaultdict

import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset
import numpy as np
import torchvision.models as models
from transformers import AutoTokenizer, AutoModel, PreTrainedModel, PretrainedConfig
import pandas as pd

from medclip.evaluator import Evaluator
from medclip.dataset import ZeroShotImageDataset
from medclip.dataset import ZeroShotImageCollator
from medclip.prompts import generate_class_prompts, generate_chexpert_class_prompts, generate_covid_class_prompts
from medclip import constants

# set random seed
seed = 42
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
os.environ['PYTHONASHSEED'] = str(seed)

class ConVIRT(nn.Module):
    def __init__(self):
        super().__init__()

        # load two encoders
        self.image_model = models.resnet50(pretrained=True)
        num_fts = self.image_model.fc.in_features
        self.image_model.fc = nn.Linear(num_fts, 512) # projection head
        
        self.text_model = AutoModel.from_pretrained("emilyalsentzer/Bio_ClinicalBERT")
        self.text_projection_head = nn.Linear(768, 512)

        self.tokenizer = AutoTokenizer.from_pretrained("emilyalsentzer/Bio_ClinicalBERT")

        # hyperparameter
        self.lamb = 0.75
        self.temperature = 0.1

    def forward(self,
        input_ids=None,
        pixel_values=None,
        attention_mask=None,
        return_loss=True,
        **kwargs):
        input_ids = input_ids.cuda()
        attention_mask = attention_mask.cuda()
        pixel_values = pixel_values.cuda()

        # image encoding
        img_embed = self.image_model(pixel_values)
        img_embed = img_embed / img_embed.norm(dim=-1, keepdim=True)

        # text encoding
        text_embed = self.text_model(input_ids=input_ids, attention_mask=attention_mask)
        text_embed = text_embed['pooler_output']
        text_embed = self.text_projection_head(text_embed)
        text_embed = text_embed / text_embed.norm(dim=-1, keepdim=True)

        logits = torch.matmul(img_embed, text_embed.t()) / self.temperature

        outputs = {
            'img_embeds':img_embed, 'text_embeds':text_embed,
            'logits':logits, 'logits_per_text':logits.T, 'loss_value':None
        }
        if return_loss:
            # compute infonce loss
            outputs['loss_value'] = self.compute_loss(logits)
        return outputs
        
    def compute_loss(self, logits):
        loss_fn = nn.CrossEntropyLoss()
        image_loss = loss_fn(logits, torch.arange(len(logits), device=logits.device))
        caption_loss = loss_fn(logits.T, torch.arange(len(logits.T), device=logits.device))
        loss_value = self.lamb * image_loss + (1-self.lamb) * caption_loss
        return loss_value

class ConVIRTClassifier(nn.Module):
    def __init__(self, model, ensemble=False):
        super().__init__()
        self.model = model
        self.ensemble = ensemble
    
    def forward(self, pixel_values=None, prompt_inputs=None, **kwargs):
        pixel_values = pixel_values.cuda()
        class_similarities = []
        class_names = []
        for cls_name, cls_text in prompt_inputs.items():
            inputs = {'pixel_values':pixel_values}
            for k in cls_text.keys(): inputs[k] = cls_text[k].cuda()

            medclip_outputs = self.model(**inputs, return_loss=False)
            logits = medclip_outputs['logits']

            # take logits max as the class similarity
            if self.ensemble:
                cls_sim = torch.mean(logits, 1) # equivalent to prompt ensembling
            else:
                cls_sim = torch.max(logits, 1)[0]
            class_similarities.append(cls_sim)
            class_names.append(cls_name)
        
        class_similarities = torch.stack(class_similarities, 1)
        outputs = {
            'logits': class_similarities,
            'class_names': class_names,
        }
        return outputs

os.environ['CUDA_VISIBLE_DEVICES']='0'
model = ConVIRT()
path = os.path.join('./checkpoints/convirt_pretrain/37000/pytorch_model.bin')
state_dict = torch.load(path)
model.load_state_dict(state_dict)
model.cuda()

# build evaluator
# val_data = ZeroShotImageDataset(['chexpert-5x200'])
val_data = ZeroShotImageDataset(['mimic-5x200'])
# val_data = ZeroShotImageDataset(['iuxray-5x200'])
# val_data = ZeroShotImageDataset(['covid19-test'])

# generate class prompts by sampling from sentences
df_sent = pd.read_csv('./local_data/sentence-label.csv', index_col=0)

acc_list = []
for i in range(5):
    # ##########
    # for covid19 data
    # cls_prompts = generate_class_prompts(df_sent, ['No Finding'], n=10)
    # covid_prompts = generate_covid_class_prompts(n=10)
    # cls_prompts.update(covid_prompts) 
    # val_collate_fn = ZeroShotImageCollator(cls_prompts=cls_prompts)
    # eval_dataloader = DataLoader(val_data,
    #     batch_size=128,
    #     collate_fn=val_collate_fn,
    #     shuffle=False,
    #     pin_memory=True,
    #     num_workers=0,
    #     )
    # medclip_clf = ConVIRTClassifier(model, ensemble=True)
    # medclip_clf.cuda()
    # evaluator = Evaluator(
    #     medclip_clf=medclip_clf,
    #     eval_dataloader=eval_dataloader,
    #     class_names=constants.COVID_TASKS,
    # )
    # ##########

    # ##########
    # for chexpert and iuxray
    cls_prompts = generate_chexpert_class_prompts(n=10)
    val_collate_fn = ZeroShotImageCollator(cls_prompts=cls_prompts)
    eval_dataloader = DataLoader(val_data,
        batch_size=128,
        collate_fn=val_collate_fn,
        shuffle=False,
        pin_memory=True,
        num_workers=0,
        )
    medclip_clf = ConVIRTClassifier(model, ensemble=True)
    medclip_clf.cuda()
    evaluator = Evaluator(
        medclip_clf=medclip_clf,
        eval_dataloader=eval_dataloader,
        class_names=constants.CHEXPERT_COMPETITION_TASKS,
    )
    # ##########

    res = evaluator.evaluate()['acc']
    acc_list.append(res)
    print(res)


print('mean: {:.4f}, std: {:.2f}'.format(np.mean(acc_list), np.std(acc_list)))