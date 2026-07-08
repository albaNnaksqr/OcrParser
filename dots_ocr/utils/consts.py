MIN_PIXELS = 3136
MAX_PIXELS = 11289600
# Hard ceiling imposed by upstream OpenAI-compatible servers to avoid decompression bomb checks.
# The upstream limit is 178,956,970 pixels; we stay slightly under to avoid boundary rounding issues.
ABSOLUTE_MAX_PIXELS = 178_900_000
IMAGE_FACTOR = 28

image_extensions = {'.jpg', '.jpeg', '.png'}
