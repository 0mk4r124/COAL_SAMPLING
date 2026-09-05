import logging
import os
import traceback

from logging.handlers import TimedRotatingFileHandler


class SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """
    TimedRotatingFileHandler that survives Windows file locking.

    Several services import the same module (LOGIC.py is imported by both
    MAIN_MANAGER and RFID_READER), so more than one process can hold the same
    log file open. At midnight each tries to rename it and Windows refuses,
    because a file open in another process cannot be renamed. The stock
    handler lets that PermissionError escape, and the log line is dropped.

    Here a failed rollover is swallowed: the current file keeps growing and
    the next process to roll over succeeds. Losing a day boundary in the log
    is far better than losing the log lines themselves.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError) as e:
            # Push the next attempt forward so we don't retry on every record
            try:
                self.rolloverAt = self.computeRollover(int(self.rolloverAt))
            except Exception:
                pass
            print(f"[LOGGING] Rollover skipped for {self.baseFilename}: {e}")


def initializeLogger(logger_name, LOGS_PATH="LOGS"):
    logger = None

    try:
        LOG_BACKUP_COUNT = 2
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)

        # Called more than once for the same name (LOGIC.py is imported by
        # several scripts) it would otherwise stack handlers and write every
        # line two or three times.
        if logger.handlers:
            return logger

        os.makedirs(LOGS_PATH, exist_ok=True)

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        log_file = os.path.join(LOGS_PATH, f"{logger_name}.log")

        file_handler = SafeTimedRotatingFileHandler(
            log_file, when='midnight', backupCount=LOG_BACKUP_COUNT,
            encoding='utf-8',     # the '→' and '—' in log messages need this
            delay=True            # open on first write, not at import
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.propagate = False

        logger.debug(f"Logger initialized for {logger_name}")
    except Exception as e:
        print(f"initializeLogger() Exception for {logger_name}: {e}")
        print(traceback.format_exc())

    return logger