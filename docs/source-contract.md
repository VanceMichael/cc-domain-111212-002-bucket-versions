# 来源格式与业务语义

本地 HTTP 采用 S3 版本清单的 JSON 投影，不宣称覆盖完整 S3 鉴权。GET /list?key-marker=...&version-id-marker=... 返回 entries、isTruncated 和两个下一标记；key 按 encoding 解码一次，versionId 不透明。GET /object?key=...&versionId=... 返回具体版本，x-object-version-id 与 x-checksum-sha256 用于验证；423 表示归档取回中，404 表示该版本不可用。删除标记没有对象正文。提供的 ETag 刻意含分段后缀，不能用作 MD5。

## 清单采集约定

- 续页必须把来源返回的 nextKeyMarker 与 nextVersionIdMarker 成对作为下一页的 key-marker、version-id-marker 透传；只传一个标记会重复或漏页。截断页缺少任一标记按协议错误处理，不能把失败页当成空清单。
- 同一键的版本允许跨页出现。条目以（观察批次、键、versionId）唯一留存，不按 LastModified 去重，也不按时间推断“唯一版本”。
- 删除标记（kind=delete）与普通版本同等保留，它不是空文件，也不允许发起对象取回；当前集合里它单列在 current_delete_markers。
- isLatest 是服务端在该页返回时的观察值，落库即冻结。随后在更老的页里抓到旧版本，不回退、不改写此前记录的 isLatest 指针。
- 暂停（达到页数上限或来源仍截断）只保存双标记；重开时新建观察批次并以 resume_of 关联前一批次。批次是独立观察，不把多批次拼成一次原子快照。
- 每个清单页的原始字节、请求 URL、sha256 摘要和页内条目一并留存，保证可追溯到具体来源响应。

## 版本取回与核实

- 取对象必须在查询串携带 versionId，按键不带版本取到的“当前版本”不能用于历史版本归档。
- 200 响应先核对 x-object-version-id 与请求 versionId，再用 x-checksum-sha256（以及清单声明的 checksum）与正文 sha256 比对。ETag 可能是分段上传产生的非内容摘要，任何情况下都不参与内容核实。
- 取回状态：423 记为 restore-pending（归档取回中，等待而非缺失）；404 才记为 missing；200 且校验通过为 verified（此时才留存正文）；200 但没有任何 sha256 证据为 received-unverified（收到但未核实）。尚未取回的清单条目标记为 manifest-only，不能误报为缺失。

本地演练夹具由 `sampledata.py` 生成，均为虚构业务数据。`sample` 是当前可查看输入，`invalid` 是边界失败输入，其余用于对照来源变体。生成不会下载资源。

本地 HTTP 演练器采用方法与原始路径作为路由键，值为按调用次数返回的响应数组，耗尽后维持最后一项。响应可定义 status、headers、text/base64、require_headers、chunk_bytes、disconnect_after。每次请求保留方法、路径、头与正文摘要表示，便于核对实际访问；演练器只监听回环地址。

原始输入上限 16 MiB 是现有查看入口的保护值，不代表未来持续采集需一次读完整来源。格式含义和截断错误不能被 HTTP 200 覆盖。
