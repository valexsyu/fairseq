# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math

import torch
import torch.nn.functional as F
from fairseq import metrics, utils
from fairseq.criterions import FairseqCriterion, register_criterion
from fairseq.dataclass import FairseqDataclass
from torch import Tensor
from fairseq.criterions.nat_loss import (
    LabelSmoothedDualImitationCriterion, 
    LabelSmoothedDualImitationCriterionConfig,
)
from fairseq.criterions import nat_loss 
from dataclasses import dataclass, field





@register_criterion("nat_ctc_loss", dataclass=LabelSmoothedDualImitationCriterionConfig)
class NatEncoderCTCLoss(LabelSmoothedDualImitationCriterion):

    def __init__(self, task, label_smoothing):
        super().__init__(task, label_smoothing)
        self.pad_idx = task.target_dictionary.pad()
        self.eos_idx = task.target_dictionary.eos()    
        self.bos_idx = task.target_dictionary.bos()
        if task.cfg.blank_use_mask :
            if '[MASK]' in task.target_dictionary.symbols :
                self.blank_idx = task.target_dictionary.indices['[MASK]']
            elif '<mask>' in task.target_dictionary.symbols :
                self.blank_idx = task.target_dictionary.indices['<mask>']            
            else :
                import pdb;pdb.set_trace()
                print("check the MASK token symbol")
        else:
            self.blank_idx = (
                task.target_dictionary.index(task.blank_symbol)
                if hasattr(task, "blank_symbol")
                else self.bos_idx
            )        
        
    @classmethod
    def add_args(cls, parser):
        LabelSmoothedDualImitationCriterion.add_args(parser)
       
    
    def _compute_ctc_loss(  #valex
        self, lprobs, targets, masks=None, num_upsampling_rate=2, name="loss", factor=1.0, sample=None, reduction="mean"
    ):
        """
        outputs: batch x len x d_model
        targets: batch x len
        masks:   batch x len

        policy_logprob: if there is some policy
            depends on the likelihood score as rewards.
        """
        def mean_ds(x: Tensor, dim=None) -> Tensor:
            return (
                x.float().mean().type_as(x)
                if dim is None
                else x.float().mean(dim).type_as(x)
            )
        
        
        lprobs = lprobs.contiguous()
        # lprobs = model.get_normalized_probs(
        #     [outputs], log_probs=True
        # ).contiguous()  # (T, B, C) from the encoder 
        
        if "src_lengths" in sample["net_input"]:
            input_lengths = sample["net_input"]["src_lengths"]
            input_lengths_upsample = (num_upsampling_rate*input_lengths).type_as(input_lengths) 
        else:
            input_lengths = lprobs.new_full(
                (lprobs.size(1),), lprobs.size(0), dtype=torch.long
            )
        
        pad_mask = (targets != self.pad_idx) & (
                    targets != self.eos_idx) & (
                    targets != self.bos_idx)
                    
        targets_flat = targets.masked_select(pad_mask)
        if "target_lengths" in sample:
            target_lengths = sample["target_lengths"]
        else:
            target_lengths = pad_mask.sum(-1)

        lprobs = lprobs.transpose(0,1)    

 
        with torch.backends.cudnn.flags(enabled=False):
            loss = F.ctc_loss(
                lprobs,
                targets_flat,
                input_lengths_upsample,
                target_lengths,
                blank=self.blank_idx, 
                reduction=reduction,
                zero_infinity=True,
            )           
            
        loss = loss * factor
        nll_loss = loss
        
        return {"name": name, "loss": loss, "nll_loss": nll_loss, "factor": factor}
    
    def _compute_loss(
        self, outputs, targets, masks=None, label_smoothing=0.0, name="loss", factor=1.0
    ):
        """
        outputs: batch x len x d_model
        targets: batch x len
        masks:   batch x len

        policy_logprob: if there is some policy
            depends on the likelihood score as rewards.
        """

        def mean_ds(x: Tensor, dim=None) -> Tensor:
            return (
                x.float().mean().type_as(x)
                if dim is None
                else x.float().mean(dim).type_as(x)
            )

        if masks is not None:
            outputs, targets = outputs[masks], targets[masks]

        if masks is not None and not masks.any():
            nll_loss = torch.tensor(0)
            loss = nll_loss
        else:
            logits = F.log_softmax(outputs, dim=-1)
            if targets.dim() == 1:
                losses = F.nll_loss(logits, targets.to(logits.device), reduction="none")

            else:  # soft-labels
                losses = F.kl_div(logits, targets.to(logits.device), reduction="none")
                losses = losses.sum(-1)

            nll_loss = mean_ds(losses)
            if label_smoothing > 0:
                loss = (
                    nll_loss * (1 - label_smoothing) - mean_ds(logits) * label_smoothing
                )
            else:
                loss = nll_loss

        loss = loss * factor
        return {"name": name, "loss": loss, "nll_loss": nll_loss, "factor": factor}

    def _compute_mse_loss(
        self, outputs, targets, masks=None, label_smoothing=0.0, name="loss", factor=1.0
    ):
        """
        outputs: batch x len x d_model
        targets: batch x len
        masks:   batch x len

        policy_logprob: if there is some policy
            depends on the likelihood score as rewards.
        """

        def mean_ds(x: Tensor, dim=None) -> Tensor:
            return (
                x.float().mean().type_as(x)
                if dim is None
                else x.float().mean(dim).type_as(x)
            )

        if masks is not None:
            outputs, targets = outputs[masks], targets[masks]

        
        nll_loss = F.mse_loss(outputs,targets,reduction='sum')
        loss = nll_loss

        loss = loss * factor
        return {"name": name, "loss": loss, "nll_loss": nll_loss, "factor": factor}

    def _custom_loss(self, loss, name="loss", factor=1.0):
        return {"name": name, "loss": loss, "factor": factor}
    def _custom_value(self, value, name="num", factor=1.0):
        return {"name": name, "value": value, "factor": factor}    

    def forward(self, model, sample, update_num , pretrained_lm=None, lm_loss_layer=-1, reduce=True):
        """Compute the loss for the given sample.
        Returns a tuple with three elements:
        1) the loss
        2) the sample size, which is used as the denominator for the gradient
        3) logging outputs to display while training
        """
        nsentences, ntokens = sample["nsentences"], sample["ntokens"]

        # B x T
        src_tokens, src_lengths = (
            sample["net_input"]["src_tokens"],
            sample["net_input"]["src_lengths"],
        )
        #tgt_tokens, prev_output_tokens = sample["target"], sample["prev_target"]
        if sample.get("alignments", None) is not None: 
            tgt_tokens , alignments= sample["target"], sample["alignments"]
        else:
            tgt_tokens = sample["target"]
            alignments = None
        outputs = model(src_tokens, src_lengths, tgt_tokens, alignments, update_num, pretrained_lm, lm_loss_layer)
        
        losses, nll_loss = [], []

        for obj in outputs:
            if outputs[obj].get("loss_type", "CTC") == "CTC":
                _losses = self._compute_ctc_loss(
                    outputs[obj].get("out"),
                    outputs[obj].get("tgt"),
                    outputs[obj].get("mask", None),
                    outputs[obj].get("num_upsampling_rate", 2), 
                    name=obj + "-loss",
                    factor=1.0,
                    sample=sample,
                    
                )
            elif outputs[obj].get("loss_type", "CTC") == "MSE":
                _losses = self._compute_mse_loss(
                    outputs[obj].get("out"),
                    outputs[obj].get("tgt"),
                    outputs[obj].get("mask", None),
                    outputs[obj].get("ls", 0.0),
                    name=obj + "-loss",
                    factor=outputs[obj].get("factor", 1.0),
                )     
            else:
                _losses = self._custom_loss(
                    outputs[obj].get("loss"),
                    name=obj + "-loss",
                    factor=outputs[obj].get("factor", 1.0),
                )

            losses += [_losses]
            if outputs[obj].get("nll_loss", False):
                nll_loss += [_losses.get("nll_loss", 0.0)]
        loss = sum(l["loss"] for l in losses)
        nll_loss = sum(l for l in nll_loss) if len(nll_loss) > 0 else loss.new_tensor(0)

        # NOTE:
        # we don't need to use sample_size as denominator for the gradient
        # here sample_size is just used for logging
        sample_size = 1        
        logging_output = {
            "loss": loss.data,
            "nll_loss": nll_loss.data,
            "ntokens": ntokens,
            "nsentences": nsentences,
            "sample_size": sample_size,
        }

        for l in losses:
            logging_output[l["name"]] = (
                utils.item(l["loss"].data / l["factor"])
                if reduce
                else l[["loss"]].data / l["factor"]
            )

        return loss, sample_size, logging_output

    @staticmethod
    def reduce_metrics(logging_outputs) -> None:
        """Aggregate logging outputs from data parallel training."""
        sample_size = utils.item(
            sum(log.get("sample_size", 0) for log in logging_outputs)
        )
        loss = utils.item(sum(log.get("loss", 0) for log in logging_outputs))
        nll_loss = utils.item(sum(log.get("nll_loss", 0) for log in logging_outputs))

        metrics.log_scalar(
            "loss", loss / sample_size / math.log(2), sample_size, round=3
        )
        metrics.log_scalar(
            "nll_loss", nll_loss / sample_size / math.log(2), sample_size, round=3
        )
        metrics.log_derived(
            "ppl", lambda meters: utils.get_perplexity(meters["loss"].avg)
        )

        for key in logging_outputs[0]:
            if key[-5:] == "-loss":
                val = sum(log.get(key, 0) for log in logging_outputs)
                metrics.log_scalar(
                    key[:-5],
                    val / sample_size / math.log(2) if sample_size > 0 else 0.0,
                    sample_size,
                    round=3,
                )

    @staticmethod
    def logging_outputs_can_be_summed() -> bool:
        """
        Whether the logging outputs returned by `forward` can be summed
        across workers prior to calling `reduce_metrics`. Setting this
        to True will improves distributed training speed.
        """
        return True
    
    
    
@register_criterion("nat_ctc_sel_rate_loss", dataclass=LabelSmoothedDualImitationCriterionConfig)
class NatCTCSelRateLoss(NatEncoderCTCLoss):
    def __init__(self, task, label_smoothing):
        super().__init__(task, label_smoothing)
        # self.rate_list = task.cfg.rate_list
        self.max_update = task.cfg.max_update
        self.lmax_only_step = task.cfg.lmax_only_step
    def _compute_ctc_loss(  #valex
        self, lprobs, targets, masks=None, num_upsampling_rate=2, name="loss", factor=1.0, sample=None, reduction="mean"
    ):
        """
        outputs: batch x len x d_model
        targets: batch x len
        masks:   batch x len

        policy_logprob: if there is some policy
            depends on the likelihood score as rewards.
        """
        def mean_ds(x: Tensor, dim=None) -> Tensor:
            return (
                x.float().mean().type_as(x)
                if dim is None
                else x.float().mean(dim).type_as(x)
            )
        
        
        lprobs = lprobs.contiguous()
        # lprobs = model.get_normalized_probs(
        #     [outputs], log_probs=True
        # ).contiguous()  # (T, B, C) from the encoder 
        
        if "src_lengths" in sample["net_input"]:
            input_lengths = sample["net_input"]["src_lengths"]
            input_lengths_upsample = (num_upsampling_rate*input_lengths).type_as(input_lengths) 
        else:
            input_lengths = lprobs.new_full(
                (lprobs.size(1),), lprobs.size(0), dtype=torch.long
            )
        
        pad_mask = (targets != self.pad_idx) & (
                    targets != self.eos_idx) & (
                    targets != self.bos_idx)
                    
        targets_flat = targets.masked_select(pad_mask)
        if "target_lengths" in sample:
            target_lengths = sample["target_lengths"]
        else:
            target_lengths = pad_mask.sum(-1)

        lprobs = lprobs.transpose(0,1)    

        
        with torch.backends.cudnn.flags(enabled=False):
            loss = F.ctc_loss(
                lprobs,
                targets_flat,
                input_lengths_upsample,
                target_lengths,
                blank=self.blank_idx, 
                reduction=reduction,
                zero_infinity=True,
            )        

        loss = loss * factor
        nll_loss = loss
        
        return {"name": name, "loss": loss, "nll_loss": nll_loss, "factor": factor, "tgt_lengths":target_lengths}

    def forward(self, model, sample, update_num , pretrained_lm=None, lm_loss_layer=-1, reduce=True):
        """Compute the loss for the given sample.
        Returns a tuple with three elements:
        1) the loss
        2) the sample size, which is used as the denominator for the gradient
        3) logging outputs to display while training
        """
        nsentences, ntokens = sample["nsentences"], sample["ntokens"]

        # B x T
        src_tokens, src_lengths = (
            sample["net_input"]["src_tokens"],
            sample["net_input"]["src_lengths"],
        )
        #tgt_tokens, prev_output_tokens = sample["target"], sample["prev_target"]
        if sample.get("alignments", None) is not None: 
            tgt_tokens , alignments= sample["target"], sample["alignments"]
        else:
            tgt_tokens = sample["target"]
            alignments = None
        
        losses, nll_loss = [], []
        ctc_losses = []
        others_losses = []
        
        
        # for upsampling_rate in self.rate_list : 
        for upsampling_rate in [2,3,4] :             
            outputs = model(src_tokens, src_lengths, tgt_tokens, alignments, update_num, 
                            pretrained_lm, lm_loss_layer, upsampling_rate)
            for obj in outputs:
                if outputs[obj].get("loss_type", "CTC") == "CTC":
                    _ctc_losses = self._compute_ctc_loss(
                        outputs[obj].get("out"),
                        outputs[obj].get("tgt"),
                        outputs[obj].get("mask", None),
                        outputs[obj].get("num_upsampling_rate", 2), 
                        name=obj + "-loss",
                        factor=1.0,
                        sample=sample,
                        reduction='none',
                        
                    )
                else:
                    _others_losses = self._custom_loss(
                        outputs[obj].get("loss"),
                        name=obj + "-loss",
                        factor=outputs[obj].get("factor", 1.0),
                    )
            ctc_losses +=[_ctc_losses]
            # others_losses +=[_others_losses]
        
            
        ctc_losses = torch.stack([i['loss'] for i in ctc_losses], dim = 0) 
        # div_tgt_lengths = torch.stack([i['tgt_lengths'] for i in ctc_losses], dim = 0)
        #leave some steps for checkpoint averaging
        time = update_num / (self.max_update - self.lmax_only_step)
        curr_lambda = 2/3
        num_rate, bz = ctc_losses.size()
        if time < curr_lambda:   
            t_1 = time / curr_lambda
            ctc_sum_loss = ctc_losses.mean()  # K x B size
            ctc_lse_loss = - torch.sum(torch.logsumexp(-ctc_losses, dim = 0)) / bz
            loss = t_1 * ctc_lse_loss + (1 - t_1) * ctc_sum_loss                    
        elif time < 1:
            t_2 = (time - curr_lambda) / (1 - curr_lambda)
            ctc_lse_loss = - torch.sum(torch.logsumexp(-ctc_losses, dim = 0)) / bz
            ctc_min_loss, min_idx = torch.min(ctc_losses, dim = 0)
            ctc_min_loss = ctc_min_loss.mean()    
            loss = t_2 * ctc_min_loss + (1 - t_2) * ctc_lse_loss 
        else:
            ctc_min_loss, min_idx = torch.min(ctc_losses, dim = 0)
            ctc_min_loss = ctc_min_loss.mean()    
            loss = ctc_min_loss                       
              
            # losses += [_losses]
            # if outputs[obj].get("nll_loss", False):
            #     nll_loss += [_losses.get("nll_loss", 0.0)]
                        
                
        # loss = sum(l["loss"] for l in losses)
        # nll_loss = sum(l for l in nll_loss) if len(nll_loss) > 0 else loss.new_tensor(0)
        nll_loss = loss.new_tensor(0)

        # NOTE:
        # we don't need to use sample_size as denominator for the gradient
        # here sample_size is just used for logging
        sample_size = 1        
        logging_output = {
            "loss": loss.data,
            "nll_loss": nll_loss.data,
            "ntokens": ntokens,
            "nsentences": nsentences,
            "sample_size": sample_size,
        }

        for l in losses:
            logging_output[l["name"]] = (
                utils.item(l["loss"].data / l["factor"])
                if reduce
                else l[["loss"]].data / l["factor"]
            )

        return loss, sample_size, logging_output

