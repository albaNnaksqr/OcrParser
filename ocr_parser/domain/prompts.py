from __future__ import annotations

from dots_ocr.utils.layout_utils import pre_process_bboxes
from dots_ocr.utils.prompts import dict_promptmode_to_prompt


def get_prompt(self, prompt_mode, bbox=None, origin_image=None, image=None, min_pixels=None, max_pixels=None):
    prompt = dict_promptmode_to_prompt[prompt_mode]
    if prompt_mode == "prompt_grounding_ocr":
        assert bbox is not None
        bboxes = [bbox]
        bbox = pre_process_bboxes(
            origin_image,
            bboxes,
            input_width=image.width,
            input_height=image.height,
            min_pixels=min_pixels,
            max_pixels=max_pixels,
        )[0]
        prompt = prompt + str(bbox)
    return prompt
