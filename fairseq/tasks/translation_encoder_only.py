# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from dataclasses import dataclass, field
import imp
import torch
from fairseq import utils
from fairseq.data import LanguagePairDataset
from fairseq.dataclass import ChoiceEnum
from fairseq.tasks import register_task
from fairseq.tasks.translation import (
    TranslationConfig,
    TranslationTask,
    load_langpair_dataset,
)
from fairseq.utils import new_arange


NOISE_CHOICES = ChoiceEnum(["random_delete", "random_mask", "no_noise", "full_mask"])
FREEZE_MODULE = ChoiceEnum(["reorder", "translator","None"])


@dataclass
class TranslationEncoderOnlyConfig(TranslationConfig):
    noise: NOISE_CHOICES = field(
        default="random_delete",
        metadata={"help": "type of noise"},
    )

    num_upsampling_rate: int = field(
        default=1, metadata={"help": "The multiplier value of the source upsampling "},
    )  

    random_mask_rate: float = field(
        default=0.2, metadata={"help": "The multiplier value of the source upsampling "},
    )   
    freeze_module: FREEZE_MODULE = field(
        default="None",
        metadata={"help": "choose a module to freeze"},
    )
    add_blank_symbol: bool = field(
        default=False, metadata={"help": "add the blank symbol in the target dictionary"},
    )     
    prepend_bos: bool = field(
        default=True, metadata={"help": "if set, without bos token"},
    )      
       
    global_token: bool = field(
        default=False, metadata={"help": "if set, use global_token but not calculate loss in nat_ctc_loss"},
    )     

    iterative_reorder_translator: bool = field(
        default=False, metadata={"help": "if set, iterative training reorder and translator"},
    )     
    
    iter_num_reorder: int = field(
        default=2, metadata={"help": "reorder traning after the numbers of translator  "},
    )  
    
    


@register_task("translation_encoder_only", dataclass=TranslationEncoderOnlyConfig)
class TranslationEnocderOnly(TranslationTask):
    """
    Translation (Sequence Generation) task for Levenshtein Transformer
    See `"Levenshtein Transformer" <https://arxiv.org/abs/1905.11006>`_.
    """
    cfg: TranslationEncoderOnlyConfig

    def __init__(self, cfg: TranslationEncoderOnlyConfig, src_dict, tgt_dict):
        super().__init__(cfg, src_dict, tgt_dict)
        if cfg.add_blank_symbol :
            self.blank_symbol = '<blank>'
            self.tgt_dict.add_symbol(self.blank_symbol)
            self.src_dict.add_symbol(self.blank_symbol)
            def blank_symbol(self):
                return self.blank_symbol 
        self.prepend_bos = cfg.prepend_bos
        self.freeze_module = self.cfg.freeze_module
        self.iter_num_reorder = cfg.iter_num_reorder 
        self.iterative_reorder_translator = cfg.iterative_reorder_translator
        self.switch = True

  

    def load_dataset(self, split, epoch=1, combine=False, **kwargs):
        """Load a given dataset split.

        Args:
            split (str): name of the split (e.g., train, valid, test)
        """
        paths = utils.split_paths(self.cfg.data)
        assert len(paths) > 0
        data_path = paths[(epoch - 1) % len(paths)]
        # infer langcode
        src, tgt = self.cfg.source_lang, self.cfg.target_lang
        self.datasets[split] = load_langpair_dataset(
            data_path,
            split,
            src,
            self.src_dict,
            tgt,
            self.tgt_dict,
            combine=combine,
            dataset_impl=self.cfg.dataset_impl,
            upsample_primary=self.cfg.upsample_primary,
            left_pad_source=self.cfg.left_pad_source,
            left_pad_target=self.cfg.left_pad_target,
            max_source_positions=self.cfg.max_source_positions,
            max_target_positions=self.cfg.max_target_positions,
            prepend_bos=self.prepend_bos,
        )

    def inject_noise(self, target_tokens):
        def _random_delete(target_tokens):
            pad = self.tgt_dict.pad()
            bos = self.tgt_dict.bos()
            eos = self.tgt_dict.eos()

            max_len = target_tokens.size(1)
            target_mask = target_tokens.eq(pad)
            target_score = target_tokens.clone().float().uniform_()
            target_score.masked_fill_(
                target_tokens.eq(bos) | target_tokens.eq(eos), 0.0
            )
            target_score.masked_fill_(target_mask, 1)
            target_score, target_rank = target_score.sort(1)
            target_length = target_mask.size(1) - target_mask.float().sum(
                1, keepdim=True
            )

            # do not delete <bos> and <eos> (we assign 0 score for them)
            target_cutoff = (
                2
                + (
                    (target_length - 2)
                    * target_score.new_zeros(target_score.size(0), 1).uniform_()
                ).long()
            )
            target_cutoff = target_score.sort(1)[1] >= target_cutoff

            prev_target_tokens = (
                target_tokens.gather(1, target_rank)
                .masked_fill_(target_cutoff, pad)
                .gather(1, target_rank.masked_fill_(target_cutoff, max_len).sort(1)[1])
            )
            prev_target_tokens = prev_target_tokens[
                :, : prev_target_tokens.ne(pad).sum(1).max()
            ]

            return prev_target_tokens

        def _random_mask(target_tokens):
            pad = self.tgt_dict.pad()
            bos = self.tgt_dict.bos()
            eos = self.tgt_dict.eos()
            unk = self.tgt_dict.unk()

            target_masks = (
                target_tokens.ne(pad) & target_tokens.ne(bos) & target_tokens.ne(eos)
            )
            target_score = target_tokens.clone().float().uniform_()
            target_score.masked_fill_(~target_masks, 2.0)
            target_length = target_masks.sum(1).float()
            target_length = target_length * target_length.clone().uniform_()*self.cfg.random_mask_rate
            target_length = target_length + 1  # make sure to mask at least one token.

            _, target_rank = target_score.sort(1)
            target_cutoff = new_arange(target_rank) < target_length[:, None].long()
            prev_target_tokens = target_tokens.masked_fill(
                target_cutoff.scatter(1, target_rank, target_cutoff), unk
            )
            return prev_target_tokens

        def _full_mask(target_tokens):
            pad = self.tgt_dict.pad()
            bos = self.tgt_dict.bos()
            eos = self.tgt_dict.eos()
            unk = self.tgt_dict.unk()

            target_mask = (
                target_tokens.eq(bos) | target_tokens.eq(eos) | target_tokens.eq(pad)
            )
            return target_tokens.masked_fill(~target_mask, unk)

        if self.cfg.noise == "random_delete":
            return _random_delete(target_tokens)
        elif self.cfg.noise == "random_mask":
            return _random_mask(target_tokens)
        elif self.cfg.noise == "full_mask":
            return _full_mask(target_tokens)
        elif self.cfg.noise == "no_noise":
            return target_tokens
        else:
            raise NotImplementedError

    def build_generator(self, models, args, **unused):
        # add models input to match the API for SequenceGenerator
        from fairseq.nat_encoder_generator import NATEncoderGenerator
        return NATEncoderGenerator(
            self.target_dictionary,
            self.source_dictionary,
            eos_penalty=getattr(args, "iter_decode_eos_penalty", 0.0),
            max_iter=getattr(args, "iter_decode_max_iter", 1),
            beam_size=getattr(args, "iter_decode_with_beam", 1),
            reranking=getattr(args, "iter_decode_with_external_reranker", False),
            decoding_format=getattr(args, "decoding_format", None),
            adaptive=not getattr(args, "iter_decode_force_max_iter", False),
            retain_history=getattr(args, "retain_iter_history", False),
            add_blank_symbol=self.cfg.add_blank_symbol,
        )




    def build_dataset_for_inference(self, src_tokens, src_lengths, constraints=None):
        if constraints is not None:
            # Though see Susanto et al. (ACL 2020): https://www.aclweb.org/anthology/2020.acl-main.325/
            raise NotImplementedError(
                "Constrained decoding with the translation_lev task is not supported"
            )

        return LanguagePairDataset(
            src_tokens, src_lengths, self.source_dictionary, append_bos=True
        )

    def train_step(
        self, sample, model, criterion, optimizer, update_num, ignore_grad=False
    ):  
        model.train()
        sample["src_noise_tokens"] = self.inject_noise(sample["net_input"]["src_tokens"])

        if update_num == 0 :
            model.load_pretrained_model()
            print("load_pretrained_model")
            #import wandb
            #wandb.watch(model, criterion=criterion, log='all',log_freq=3000, log_graph=(False))    

        if self.iterative_reorder_translator :             
            if update_num % self.iter_num_reorder == 0 :                 
                self.switch = not self.switch    
                if self.switch :
                    print("Freeze module : reorder")
                else:
                    print("Freeze module : translator")
                              
            if self.switch :                 
                self.freeze_module =  'reorder'             
            else :                 
                self.freeze_module = 'translator'        
            
        """ for merge only use
        cuda0 = torch.device('cuda:0')
        loss=torch.Tensor(0)
        sample_size=1
        logging_output={'loss': 0, 'sample_size' : 1}#, 'nll_loss': torch.Tensor(0.187), 'ntokens': 960, 'nsentences': 40, 'sample_size': 1, 'word_ins-loss': 0.18786469101905823}
        return loss, sample_size, logging_output
        """
        loss, sample_size, logging_output = criterion(model, sample, self.freeze_module)
        
        if ignore_grad:
            loss *= 0
        optimizer.backward(loss)
        return loss, sample_size, logging_output

    def valid_step(self, sample, model, criterion):
        model.eval()
        with torch.no_grad():
            sample["src_noise_tokens"] = self.inject_noise(sample["net_input"]["src_tokens"])
            loss, sample_size, logging_output = criterion(model, sample)
        return loss, sample_size, logging_output

     
    