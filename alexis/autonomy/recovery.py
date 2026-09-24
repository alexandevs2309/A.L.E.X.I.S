class RecoveryManager:
    def should_retry(self, attempt: int, max_attempts: int = 3) -> bool:
        return attempt < max_attempts

    def next_action(self, error: str) -> str:
        return "inspect_error_and_replan"
