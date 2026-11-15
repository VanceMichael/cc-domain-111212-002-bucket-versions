# 本地协议演练

`python3 scenario.py` 启动仅监听回环的业务来源，输出临时端口和可用路由。进程在前台运行，以 Ctrl-C 关闭。

`python3 scenario.py --export` 输出演练响应定义，可按 `docs/source-contract.md` 在独立副本增加乱序、截断、失败或来源更新情形。响应数组顺次消耗并保持最后状态；不会调用真实第三方系统。

请求者可以在测试中用 `from scenario import scenario` 和 `ReplaySource(scenario())` 获得同样来源并检查 requests，避免固定端口冲突。现有 scenario 测试核对来源实际返回值，不表示业务语义采集已实现。

## 标准场景覆盖的业务情形

- 键名编码：`cargo/ship A.csv` 含斜杠与空格，清单条目按 url 编码给出，续页请求必须重新编码为 `cargo%2Fship%20A.csv`。
- 双标记分页：第一页 isTruncated 为真并返回 nextKeyMarker/nextVersionIdMarker，第二页路由只在两个标记成对出现时命中。
- 删除后重建：合作方对同一键先放删除标记 v3（isLatest=true），再保留历史版本 v2、v1；当前集合不把删除标记当成空文件，历史版本逐条保留。
- 取回延迟：v2 第一次请求返回 423（归档取回中，显示等待），同一 URL 第二次返回带 x-object-version-id 与 x-checksum-sha256 的正文；ETag 刻意为非 MD5 值。
- 核实范围：v2 有 sha256 证据可核实；v1 仅返回正文、无校验头，只能标记 received-unverified；未取回的 new1 标记 manifest-only。

标准演练顺序是先准备依赖，再运行 `python3 -m unittest discover -s tests -v`、构建命令和 `python3 app.py demo`。新增能力演示须调用实际新增入口，不以现有结构检查输出代替；演练应选择有限输入，并在九十秒内完成。
