def queue_log(log_queue, message):
    log_queue.put(str(message))


import queue


def drain_log_queue(log_queue):
    while True:
        try:
            message = log_queue.get_nowait()
        except queue.Empty:
            break
        # ``None`` is the shutdown sentinel queued by StartedProcessRegistry.
        if message is None:
            continue
        print(message)
