# 来源格式与业务语义

本地 HTTP 采用 S3 版本清单的 JSON 投影，不宣称覆盖完整 S3 鉴权。GET /list?key-marker=...&version-id-marker=... 返回 entries、isTruncated 和两个下一标记；key 按 encoding 解码一次，versionId 不透明。GET /object?key=...&versionId=... 返回具体版本，x-object-version-id 与 x-checksum-sha256 用于验证；423 表示归档取回中，404 表示该版本不可用。删除标记没有对象正文。提供的 ETag 刻意含分段后缀，不能用作 MD5。

本地演练夹具由 `sampledata.py` 生成，均为虚构业务数据。`sample` 是当前可查看输入，`invalid` 是边界失败输入，其余用于对照来源变体。生成不会下载资源。

本地 HTTP 演练器采用方法与原始路径作为路由键，值为按调用次数返回的响应数组，耗尽后维持最后一项。响应可定义 status、headers、text/base64、require_headers、chunk_bytes、disconnect_after。每次请求保留方法、路径、头与正文摘要表示，便于核对实际访问；演练器只监听回环地址。

原始输入上限 16 MiB 是现有查看入口的保护值，不代表未来持续采集需一次读完整来源。格式含义和截断错误不能被 HTTP 200 覆盖。
