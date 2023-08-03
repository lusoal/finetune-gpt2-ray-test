from ray.job_submission import JobSubmissionClient

client = JobSubmissionClient("http://localhost:8265")

kick_off_gpt2_training = (
    "python fine_tune_gpt2_script.py"
)

submission_id = client.submit_job(
    entrypoint=kick_off_gpt2_training,
)