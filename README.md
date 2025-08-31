# markdown-image-download
Markdown图片下载器 自动下载Markdown文件中的在线图片到本地，并更新链接 支持并行下载和失败重试

# 设置根目录和文件路径
    root_folder = r"D:\yourpath\test"
修改为自己的文件夹，绝对路径
    
# 创建下载器实例
    root_folder=root_folder,
    max_workers=10,  # 并行下载线程数
    max_retries=3   # 最大重试次数
  根据服务器负载，自行设置
