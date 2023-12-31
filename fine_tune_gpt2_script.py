from transformers import GPT2LMHeadModel, GPT2Tokenizer, GPT2Config
from transformers import TextDataset, DataCollatorForLanguageModeling
from transformers import Trainer, TrainingArguments
import boto3
import ray
import wandb

@ray.remote
def download_file_from_s3(bucket_name, s3_file_name, local_file_name):
    try:
        s3_client = boto3.client('s3')

        # Download the file from the S3 bucket
        local_file_path = f"{local_file_name}"
        s3_client.download_file(bucket_name, s3_file_name, local_file_path)

        print(f"File '{s3_file_name}' downloaded successfully to '{local_file_path}'.")
        
        return local_file_path
    except Exception as e:
        print(f"Error: {str(e)}")

@ray.remote
def fine_tune_gpt2(model_name, train_file, output_dir):
    # Load GPT-2 model and tokenizer
    ray.init()
    model = GPT2LMHeadModel.from_pretrained(model_name)
    tokenizer = GPT2Tokenizer.from_pretrained(model_name)

    # Load training dataset
    train_dataset = TextDataset(
        tokenizer=tokenizer,
        file_path=train_file,
        block_size=128)
    # Create data collator for language modeling
    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer, mlm=False)
    # Set training arguments
    training_args = TrainingArguments(
        output_dir=output_dir,
        overwrite_output_dir=True,
        num_train_epochs=5,
        per_device_train_batch_size=4,
        save_steps=10_000,
        save_total_limit=2,
    )
    # Train the model
    trainer = Trainer(
        model=model,
        args=training_args,
        data_collator=data_collator,
        train_dataset=train_dataset,
    )
    trainer.train()
    # Save the fine-tuned model
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)
    ray.shutdown()
    
def main(api_key):
    print(api_key)
    # wandb.login(key=[api_key])
    output_file = "mental_health_data.txt"
    bucket_name = "testing-fine-tuning-jakhs"
    cluster_storage = "/mnt/cluster_storage"

    local_file_path = download_file_from_s3(bucket_name, f"output/{output_file}", f"{cluster_storage}/{output_file}")
    fine_tune_gpt2("gpt2", local_file_path, cluster_storage)

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python script.py <wandb_key>")
        sys.exit(1)

    wandb_key = sys.argv[1]
    print(wandb_key)
    main(wandb_key)
