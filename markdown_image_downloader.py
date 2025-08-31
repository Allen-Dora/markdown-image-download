#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Markdown图片下载器
自动下载Markdown文件中的在线图片到本地，并更新链接
支持并行下载和失败重试
"""

import os
import re
import requests
import hashlib
import time
import logging
from pathlib import Path
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import List, Tuple, Dict
from PIL import Image
import io

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('markdown_image_downloader.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class MarkdownImageDownloader:
    def __init__(self, root_folder: str, max_workers: int = 5, max_retries: int = 3):
        self.root_folder = Path(root_folder)
        self.max_workers = max_workers
        self.max_retries = max_retries
        self.download_lock = Lock()
        self.session = requests.Session()
        
        # 设置请求头，避免被服务器拒绝
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        
        # 图片URL正则表达式
        self.image_patterns = [
            r'!\[([^\]]*)\]\(([^\)]+)\)',  # ![alt](url)
            r'<img[^>]+src=["\']([^"\'>]+)["\'][^>]*>',  # <img src="url">
        ]
        
    def find_markdown_files(self) -> List[Path]:
        """递归查找所有.md文件"""
        md_files = []
        for md_file in self.root_folder.rglob('*.md'):
            md_files.append(md_file)
        logger.info(f"找到 {len(md_files)} 个Markdown文件")
        return md_files
    
    def extract_image_urls(self, content: str) -> List[Tuple[str, str]]:
        """从Markdown内容中提取图片URL"""
        urls = []
        
        # 匹配 ![alt](url) 格式
        pattern1 = r'!\[([^\]]*)\]\(([^\)]+)\)'
        matches1 = re.findall(pattern1, content)
        for alt, url in matches1:
            if url.startswith(('http://', 'https://')):
                urls.append((url, f'![{alt}]({url})'))  # (url, original_text)
        
        # 匹配 <img src="url"> 格式
        pattern2 = r'<img[^>]+src=["\']([^"\'>]+)["\'][^>]*>'
        matches2 = re.findall(pattern2, content)
        for url in matches2:
            if url.startswith(('http://', 'https://')):
                # 找到完整的img标签
                img_tag_pattern = f'<img[^>]+src=["\']' + re.escape(url) + '["\'][^>]*>'
                img_match = re.search(img_tag_pattern, content)
                if img_match:
                    urls.append((url, img_match.group(0)))
        
        return urls
    
    def sanitize_filename(self, filename: str) -> str:
        """清理文件名，移除非法字符"""
        # 移除或替换非法字符
        illegal_chars = '<>:"/\\|?*'
        for char in illegal_chars:
            filename = filename.replace(char, '_')
        
        # 限制文件名长度
        if len(filename) > 100:
            name, ext = os.path.splitext(filename)
            filename = name[:95] + ext
        
        return filename
    
    def get_image_extension(self, url: str, content_type: str = None) -> str:
        """根据URL或Content-Type获取图片扩展名"""
        # 首先尝试从URL获取扩展名
        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)
        _, ext = os.path.splitext(path)
        
        if ext.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg']:
            return ext.lower()
        
        # 如果URL没有扩展名，尝试从Content-Type获取
        if content_type:
            if 'jpeg' in content_type or 'jpg' in content_type:
                return '.jpg'
            elif 'png' in content_type:
                return '.png'
            elif 'gif' in content_type:
                return '.gif'
            elif 'webp' in content_type:
                return '.webp'
            elif 'svg' in content_type:
                return '.svg'
        
        # 默认使用.jpg
        return '.jpg'
    
    def compress_image(self, image_data: bytes, max_size_kb: int = 500) -> bytes:
        """压缩图片以减少存储空间"""
        try:
            # 如果图片小于限制，直接返回
            if len(image_data) <= max_size_kb * 1024:
                return image_data
            
            # 尝试压缩图片
            img = Image.open(io.BytesIO(image_data))
            
            # 转换RGBA到RGB
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            
            # 逐步降低质量直到满足大小要求
            for quality in range(85, 20, -5):
                output = io.BytesIO()
                img.save(output, format='JPEG', quality=quality, optimize=True)
                compressed_data = output.getvalue()
                
                if len(compressed_data) <= max_size_kb * 1024:
                    logger.info(f"图片压缩成功: {len(image_data)} -> {len(compressed_data)} bytes (质量: {quality})")
                    return compressed_data
            
            # 如果还是太大，返回最小质量的版本
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=20, optimize=True)
            return output.getvalue()
            
        except Exception as e:
            logger.warning(f"图片压缩失败: {e}，返回原始数据")
            return image_data
    
    def download_image(self, url: str, save_path: Path, retries: int = 0) -> bool:
        """下载单个图片"""
        try:
            # 如果文件已存在，跳过下载
            if save_path.exists():
                logger.info(f"图片已存在，跳过下载: {save_path.name}")
                return True
            
            logger.info(f"正在下载: {url}")
            
            # 添加随机延迟避免被限制
            time.sleep(0.5)
            
            response = self.session.get(url, timeout=30, stream=True)
            response.raise_for_status()
            
            # 获取图片数据
            image_data = response.content
            
            # 验证是否为有效图片
            if len(image_data) < 100:  # 太小的文件可能不是有效图片
                raise ValueError("下载的文件太小，可能不是有效图片")
            
            # 压缩图片（除了SVG）
            content_type = response.headers.get('content-type', '')
            if 'svg' not in content_type.lower():
                image_data = self.compress_image(image_data)
            
            # 确保目录存在
            save_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 保存图片
            with open(save_path, 'wb') as f:
                f.write(image_data)
            
            logger.info(f"下载成功: {save_path.name} ({len(image_data)} bytes)")
            return True
            
        except Exception as e:
            logger.error(f"下载失败 {url}: {e}")
            
            # 重试机制
            if retries < self.max_retries:
                logger.info(f"重试下载 ({retries + 1}/{self.max_retries}): {url}")
                time.sleep(2 ** retries)  # 指数退避
                return self.download_image(url, save_path, retries + 1)
            
            return False
    
    def generate_filename(self, url: str, content_type: str = None) -> str:
        """生成唯一的文件名"""
        # 使用URL的哈希值生成唯一文件名
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        
        # 尝试从URL获取原始文件名
        parsed_url = urlparse(url)
        original_name = os.path.basename(unquote(parsed_url.path))
        
        if original_name and '.' in original_name:
            name, ext = os.path.splitext(original_name)
            name = self.sanitize_filename(name)[:20]  # 限制长度
            if ext.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg']:
                return f"{name}_{url_hash}{ext.lower()}"
        
        # 如果无法从URL获取扩展名，使用Content-Type
        ext = self.get_image_extension(url, content_type)
        return f"image_{url_hash}{ext}"
    
    def process_markdown_file(self, md_file: Path) -> Dict[str, str]:
        """处理单个Markdown文件"""
        logger.info(f"处理文件: {md_file}")
        
        try:
            # 读取文件内容
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 提取图片URL
            image_urls = self.extract_image_urls(content)
            
            if not image_urls:
                logger.info(f"文件中没有找到在线图片: {md_file.name}")
                return {}
            
            logger.info(f"找到 {len(image_urls)} 个图片链接")
            
            # 创建图片存储文件夹
            images_folder = md_file.parent / 'images'
            images_folder.mkdir(exist_ok=True)
            
            # 下载图片并记录替换映射
            url_replacements = {}
            
            # 使用线程池并行下载
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_url = {}
                
                for url, original_text in image_urls:
                    # 生成本地文件名
                    filename = self.generate_filename(url)
                    local_path = images_folder / filename
                    
                    # 提交下载任务
                    future = executor.submit(self.download_image, url, local_path)
                    future_to_url[future] = (url, original_text, local_path)
                
                # 收集下载结果
                for future in as_completed(future_to_url):
                    url, original_text, local_path = future_to_url[future]
                    
                    try:
                        success = future.result()
                        if success:
                            # 生成相对路径
                            relative_path = f"images/{local_path.name}"
                            url_replacements[original_text] = relative_path
                        else:
                            logger.error(f"下载失败，保持原链接: {url}")
                    except Exception as e:
                        logger.error(f"下载任务异常: {e}")
            
            # 更新Markdown文件内容
            if url_replacements:
                updated_content = content
                for original_text, local_path in url_replacements.items():
                    if original_text.startswith('!['):
                        # 处理 ![alt](url) 格式
                        alt_match = re.match(r'!\[([^\]]*)\]\([^\)]+\)', original_text)
                        if alt_match:
                            alt_text = alt_match.group(1)
                            new_text = f"![{alt_text}]({local_path})"
                            updated_content = updated_content.replace(original_text, new_text)
                    else:
                        # 处理 <img> 标签格式
                        # 提取img标签的其他属性
                        img_attrs = re.findall(r'(\w+)=["\']([^"\'>]+)["\']', original_text)
                        attrs_dict = dict(img_attrs)
                        
                        # 构建新的img标签
                        new_attrs = [f'src="{local_path}"']
                        for attr, value in attrs_dict.items():
                            if attr.lower() != 'src':
                                new_attrs.append(f'{attr}="{value}"')
                        
                        new_text = f'<img {" ".join(new_attrs)}>'
                        updated_content = updated_content.replace(original_text, new_text)
                
                # 保存更新后的文件
                with open(md_file, 'w', encoding='utf-8') as f:
                    f.write(updated_content)
                
                logger.info(f"文件更新完成: {md_file.name} (替换了 {len(url_replacements)} 个图片链接)")
            
            return url_replacements
            
        except Exception as e:
            logger.error(f"处理文件失败 {md_file}: {e}")
            return {}
    
    def run(self):
        """运行主程序"""
        logger.info(f"开始处理文件夹: {self.root_folder}")
        
        # 查找所有Markdown文件
        md_files = self.find_markdown_files()
        
        if not md_files:
            logger.info("没有找到Markdown文件")
            return
        
        # 统计信息
        total_files = len(md_files)
        processed_files = 0
        total_images = 0
        
        # 处理每个文件
        for md_file in md_files:
            try:
                replacements = self.process_markdown_file(md_file)
                processed_files += 1
                total_images += len(replacements)
                
                logger.info(f"进度: {processed_files}/{total_files} 文件已处理")
                
            except Exception as e:
                logger.error(f"处理文件时发生错误 {md_file}: {e}")
        
        logger.info(f"处理完成！")
        logger.info(f"总计处理: {processed_files}/{total_files} 个文件")
        logger.info(f"总计下载: {total_images} 张图片")

def main():
    """主函数"""
    # 设置根目录和文件路径
    root_folder = r"D:\yourpath\test"
    
    # 创建下载器实例
    downloader = MarkdownImageDownloader(
        root_folder=root_folder,
        max_workers=10,  # 并行下载线程数
        max_retries=3   # 最大重试次数
    )
    
    # 运行下载器
    downloader.run()

if __name__ == "__main__":
    main()
