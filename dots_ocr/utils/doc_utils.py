import fitz
import numpy as np
import enum
from pydantic import BaseModel, Field
from PIL import Image
import cv2


class SupportedPdfParseMethod(enum.Enum):
    OCR = 'ocr'
    TXT = 'txt'


class PageInfo(BaseModel):
    """The width and height of page
    """
    w: float = Field(description='the width of page')
    h: float = Field(description='the height of page')


def fitz_doc_to_image(doc, target_dpi=200, origin_dpi=None) -> dict:
    """Convert fitz.Document to image, Then convert the image to numpy array.

    Args:
        doc (_type_): pymudoc page
        dpi (int, optional): reset the dpi of dpi. Defaults to 200.

    Returns:
        dict:  {'img': numpy array, 'width': width, 'height': height }
    """
    from PIL import Image
    mat = fitz.Matrix(target_dpi / 72, target_dpi / 72)
    pm = doc.get_pixmap(matrix=mat, alpha=False)

    if pm.width > 4500 or pm.height > 4500:
        mat = fitz.Matrix(72 / 72, 72 / 72)  # use fitz default dpi
        pm = doc.get_pixmap(matrix=mat, alpha=False)

    image = Image.frombytes('RGB', (pm.width, pm.height), pm.samples)
    return image


def is_blank_page(image, white_threshold=0.99, noise_threshold=0.001):
    """
    检测图像是否为空白页
    
    Args:
        image: PIL Image对象
        white_threshold: 白色像素比例阈值，超过此值认为是空白页
        noise_threshold: 噪声阈值，非白色像素的比例低于此值才认为是空白页
    
    Returns:
        bool: 是否为空白页
    """
    # 转换为numpy数组
    img_array = np.array(image)
    
    # 如果是RGBA图像，去掉alpha通道
    if img_array.shape[-1] == 4:
        img_array = img_array[:, :, :3]
    
    # 转换为灰度图像
    if len(img_array.shape) == 3:
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    else:
        gray = img_array
    
    # 计算白色像素的比例（假设白色像素值接近255）
    white_pixels = np.sum(gray > 240)  # 使用240作为白色的阈值
    total_pixels = gray.size
    white_ratio = white_pixels / total_pixels
    
    # 计算图像的方差（空白页方差应该很小）
    variance = np.var(gray)
    
    # 检查是否为空白页
    is_blank = (white_ratio > white_threshold) and (variance < noise_threshold * 255 * 255)
    
    return is_blank


def load_images_from_pdf(pdf_file, dpi=200, start_page_id=0, end_page_id=None, skip_blank_pages=False, 
                         white_threshold=0.99, noise_threshold=0.001) -> list:
    """
    从PDF文件加载图像，可选择跳过空白页
    
    Args:
        pdf_file: PDF文件路径
        dpi: 图像分辨率
        start_page_id: 起始页码
        end_page_id: 结束页码
        skip_blank_pages: 是否跳过空白页 (默认: False)
        white_threshold: 白色像素比例阈值 (默认: 0.99)
        noise_threshold: 噪声阈值 (默认: 0.001)
    
    Returns:
        list: PIL Image对象列表
    """
    images = []
    with fitz.open(pdf_file) as doc:
        pdf_page_num = doc.page_count
        end_page_id = (
            end_page_id
            if end_page_id is not None and end_page_id >= 0
            else pdf_page_num - 1
        )
        if end_page_id > pdf_page_num - 1:
            print('end_page_id is out of range, use images length')
            end_page_id = pdf_page_num - 1

        for index in range(0, doc.page_count):
            if start_page_id <= index <= end_page_id:
                page = doc[index]
                img = fitz_doc_to_image(page, target_dpi=dpi)
                
                # 检查是否为空白页
                if skip_blank_pages and is_blank_page(img, white_threshold, noise_threshold):
                    print(f"跳过空白页: 第 {index + 1} 页")
                    continue
                
                images.append(img)
    return images