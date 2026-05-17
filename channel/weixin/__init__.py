"""
微信通道模块。

提供通过微信ilink机器人协议收发消息的功能，
包含以下核心组件：
- weixin_channel: 通道主类，处理二维码登录、长轮询消息接收、多类型消息回复
- weixin_api: API客户端，封装HTTP请求和CDN媒体上传下载
- weixin_message: 消息解析类，将微信消息格式转换为统一ChatMessage格式
"""
