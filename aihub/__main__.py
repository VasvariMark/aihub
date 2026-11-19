import argparse
import logging
from logging.handlers import RotatingFileHandler
import os

from .config import Config, ROOT
from .coordinator import Coordinator
from .ollama import OllamaClient
from .server import make_server
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description='AIHub v0.4 — local coordinator and manual copy/paste bridge')
    parser.add_argument('--model', default='qwen3.5:9b')
    parser.add_argument('--context', type=int, default=4096)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    try:
        config = Config(model=args.model, context=args.context, port=args.port)
    except ValueError as exc:
        parser.error(str(exc))
    data = ROOT / 'data'
    data.mkdir(exist_ok=True)
    # OS-level lifetime lock prevents simultaneous app instances using this DB.
    lock = (data / 'instance.lock').open('a+b')
    lock.seek(0)
    if lock.read(1) == b'':
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        parser.error('AIHub is already running with this data directory.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s', handlers=[
        RotatingFileHandler(data/'aihub.log', maxBytes=1_000_000, backupCount=2, encoding='utf-8'), logging.StreamHandler()])
    store = Store(data/'aihub.sqlite3', config)
    client = OllamaClient(config)
    coordinator = Coordinator(store, client)
    try:
        server = make_server(config, store, coordinator, client)
    except OSError as exc:
        coordinator.close()
        lock.close()
        parser.error('Cannot bind loopback server: '+str(exc))
    store.recover()
    print(f'AIHub v0.4 manual bridge: http://127.0.0.1:{config.port}', flush=True)
    print('Local text generation only. Approve each task, then click Run. Stop with Ctrl+C.', flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print('\nFinishing accepted requests before shutdown…', flush=True)
    finally:
        server.server_close()
        coordinator.close()
        lock.close()


if __name__ == '__main__':
    main()
