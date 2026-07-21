import asyncio

from src.services.scheduler_service import SchedulerService


class _RecordingProcessService:
    def __init__(self):
        self.calls = []

    async def start_task(
        self,
        task_id: int,
        task_name: str,
        *,
        persistent_schedule: bool = False,
    ) -> bool:
        self.calls.append((task_id, task_name, persistent_schedule))
        return True


def test_scheduler_runs_tasks_as_persistent_schedule_workers():
    async def run_scenario():
        process_service = _RecordingProcessService()
        scheduler = SchedulerService(process_service)

        await scheduler._run_task(7, "dim十字绣")

        assert process_service.calls == [(7, "dim十字绣", True)]

    asyncio.run(run_scenario())
