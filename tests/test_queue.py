"""Work-queue protocol tests.

The in-memory implementation is used to verify the common prepare/put/get/ack/close
contract without Redis; Redis-specific reliability is implemented behind the same
WorkQueue interface.
"""
from minion.queue import InMemoryWorkQueue


async def test_in_memory_queue_acknowledgement():
    queue = InMemoryWorkQueue()
    await queue.prepare()
    await queue.put("task_1")
    item = await queue.get()
    assert item.task_id == "task_1"
    await queue.ack(item)
    await queue.close()
