import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# Rotate at 20MB × 5 backups = ~100MB max disk usage. Long live thường log 5-15MB/giờ.
fhandler = RotatingFileHandler(
    'livetalking.log',
    maxBytes=20 * 1024 * 1024,
    backupCount=5,
    encoding='utf-8',
)
fhandler.setFormatter(formatter)
fhandler.setLevel(logging.INFO)
logger.addHandler(fhandler)
