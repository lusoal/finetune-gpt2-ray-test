import ray
from datasets import load_dataset
import ray.data
from transformers import AutoTokenizer
from ray.data.preprocessors import BatchMapper
import pandas as pd
import evaluate
from transformers import Trainer, TrainingArguments
from transformers import (
    GPTJForCausalLM,
    AutoTokenizer,
    default_data_collator,
)
from transformers.utils.logging import disable_progress_bar, enable_progress_bar
import torch
from ray.air import session
import os
from ray.train.huggingface import TransformersTrainer
from ray.air import RunConfig, ScalingConfig
from ray.data.preprocessors import Chain

# Global Variables
model_name = "EleutherAI/gpt-j-6B"
use_gpu = True
num_workers = 9
cpus_per_worker = 8
storage_path="s3://testing-fine-tuning-jakhs"

ray.init(
    runtime_env={
        "pip": [
            "datasets",
            "evaluate",
            # Latest combination of accelerate==0.19.0 and transformers==4.29.0
            # seems to have issues with DeepSpeed process group initialization,
            # and will result in a batch_size validation problem.
            "accelerate==0.16.0",
            "transformers==4.26.0",
            "torch>=1.12.0",
            "deepspeed==0.9.2",
        ]
    }
)

# Testing ray Autoscaling
ray.autoscaler.sdk.request_resources(num_cpus=72)

# TBD: Get data from Amazon S3 instead of loading from local disk
print("Loading tiny_shakespeare dataset")

current_dataset = load_dataset("tiny_shakespeare")
ray_datasets_train = ray.data.from_huggingface(current_dataset['train'])
ray_datasets_validation = ray.data.from_huggingface(current_dataset['validation'])
ray_datasets_test = ray.data.from_huggingface(current_dataset['test'])

block_size = 512


def split_text(batch: pd.DataFrame) -> pd.DataFrame:
    text = list(batch["text"])
    flat_text = "".join(text)
    split_text = [
        x.strip()
        for x in flat_text.split("\n")
        if x.strip() and not x.strip()[-1] == ":"
    ]
    return pd.DataFrame(split_text, columns=["text"])


def tokenize(batch: pd.DataFrame) -> dict:
    tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=False)
    tokenizer.pad_token = tokenizer.eos_token
    ret = tokenizer(
        list(batch["text"]),
        truncation=True,
        max_length=block_size,
        padding="max_length",
        return_tensors="np",
    )
    ret["labels"] = ret["input_ids"].copy()
    return dict(ret)


splitter = BatchMapper(split_text, batch_format="pandas")
tokenizer = BatchMapper(tokenize, batch_format="pandas")

def trainer_init_per_worker(train_dataset, eval_dataset=None, **config):
    # Use the actual number of CPUs assigned by Ray
    os.environ["OMP_NUM_THREADS"] = str(
        session.get_trial_resources().bundles[-1].get("CPU", 1)
    )
    # Enable tf32 for better performance
    torch.backends.cuda.matmul.allow_tf32 = True

    batch_size = config.get("batch_size", 4)
    epochs = config.get("epochs", 2)
    warmup_steps = config.get("warmup_steps", 0)
    learning_rate = config.get("learning_rate", 0.00002)
    weight_decay = config.get("weight_decay", 0.01)

    deepspeed = {
        "fp16": {
            "enabled": "auto",
            "initial_scale_power": 8,
        },
        "bf16": {"enabled": "auto"},
        "optimizer": {
            "type": "AdamW",
            "params": {
                "lr": "auto",
                "betas": "auto",
                "eps": "auto",
            },
        },
        "zero_optimization": {
            "stage": 3,
            "offload_optimizer": {
                "device": "cpu",
                "pin_memory": True,
            },
            "offload_param": {
                "device": "cpu",
                "pin_memory": True,
            },
            "overlap_comm": True,
            "contiguous_gradients": True,
            "reduce_bucket_size": "auto",
            "stage3_prefetch_bucket_size": "auto",
            "stage3_param_persistence_threshold": "auto",
            "gather_16bit_weights_on_model_save": True,
            "round_robin_gradients": True,
        },
        "gradient_accumulation_steps": "auto",
        "gradient_clipping": "auto",
        "steps_per_print": 10,
        "train_batch_size": "auto",
        "train_micro_batch_size_per_gpu": "auto",
        "wall_clock_breakdown": False,
    }

    print("Preparing training arguments")
    training_args = TrainingArguments(
        "output",
        per_device_train_batch_size=batch_size,
        logging_steps=1,
        save_strategy="no",
        per_device_eval_batch_size=batch_size,
        learning_rate=learning_rate,
        weight_decay=weight_decay,
        warmup_steps=warmup_steps,
        label_names=["input_ids", "attention_mask"],
        num_train_epochs=epochs,
        push_to_hub=False,
        disable_tqdm=True,  # declutter the output a little
        fp16=True,
        gradient_checkpointing=True,
        deepspeed=deepspeed,
    )
    disable_progress_bar()

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    tokenizer.pad_token = tokenizer.eos_token

    print("Loading model")

    model = GPTJForCausalLM.from_pretrained(model_name, use_cache=False)
    model.resize_token_embeddings(len(tokenizer))

    print("Model loaded")

    enable_progress_bar()

    metric = evaluate.load("accuracy")

    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        predictions = np.argmax(logits, axis=-1)
        return metric.compute(predictions=predictions, references=labels)

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        compute_metrics=compute_metrics,
        tokenizer=tokenizer,
        data_collator=default_data_collator,
    )
    return trainer

trainer = TransformersTrainer(
    trainer_init_per_worker=trainer_init_per_worker,
    trainer_init_config={
        "batch_size": 16,  # per device
        "epochs": 1,
    },
    scaling_config=ScalingConfig(
        num_workers=num_workers,
        use_gpu=use_gpu,
        resources_per_worker={"GPU": 1, "CPU": cpus_per_worker},
    ),
    datasets={"train": ray_datasets_train, "evaluation": ray_datasets_validation},
    preprocessor=Chain(splitter, tokenizer),
    run_config=RunConfig(storage_path=storage_path),
)

results = trainer.fit()
ray.shutdown()
