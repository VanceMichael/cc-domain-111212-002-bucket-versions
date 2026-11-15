# bucket-versions

对象存储版本清单的完整接收所用的来源检查、版本采集与原件留存工具。

## 当前能力

`source.py` 查看来源单页的现有结构，不把检查结果冒充完整业务采集。
`collector.py` 按来源契约用 key-marker 与 version-id-marker 双标记续页，保留同键各版本及删除标记，支持暂停后按双标记另开观察批次；取回对象时强制携带 versionId，按版本响应头与 sha256 校验值核实，ETag 不参与内容摘要判断。
`storage.py` 在本地 SQLite 同时留存原始字节、清单页、版本条目、观察批次与取回状态：同一键版本可跨页，IsLatest 按落库时的服务端观察冻结；删除标记不是空文件，423 显示等待，未取回条目标记为仅在清单发现而不误报缺失。
`sampledata.py` 构造可重复的本地数据；`transport.py` 提供独立的回环 HTTP 演练源与请求记录；`scenario.py` 展示键名编码、双标记分页、删除后重建与归档取回延迟。
解释范围和来源变体见 `docs/source-contract.md`，当前检查测试与业务采集测试分文件保留。

## 环境

Python 3.11 或以上，仅标准库，无需凭据和外部数据。使用容器时可执行 `docker build -t bucket-versions .`，依赖及镜像须在正式运行前准备。
运行入口、测试与演示均不安装包，不访问公网。数据库使用临时路径，不向代码目录写入业务文件。

## 测试与构建

构建工具版本固定在 `requirements-build.txt`，在环境准备阶段执行 `python3 -m pip install -r requirements-build.txt`；容器构建已包含此步骤。应用运行仍仅使用标准库，测试及演示不安装依赖。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py source.py sampledata.py storage.py transport.py collector.py scenario.py
python3 -m build --wheel --no-isolation
```

## 快速演示

```sh
python3 app.py demo
```

演示真实启动本地来源，完成两页清单采集、删除标记保留、v2 版本 423 等待后核实、v1 无校验证据仅标记收到，并导出当前文件集合与历史版本状态，退出后自动清理。正常在数秒内完成，不以预录日志代替执行。

## 版本清单采集与导出

```sh
python3 scenario.py            # 终端 A：启动回环来源，记下输出的 url
python3 app.py observe <url> --database /tmp/bv.sqlite3 --source partner
python3 app.py observe <url> --database /tmp/bv.sqlite3 --source partner --max-pages 1
python3 app.py resume 1 --database /tmp/bv.sqlite3
python3 app.py observations --database /tmp/bv.sqlite3
python3 app.py observation 1 --database /tmp/bv.sqlite3
python3 app.py fetch-object 1 --key 'cargo/ship A.csv' --version-id v2 --database /tmp/bv.sqlite3
python3 app.py export-current 1 --database /tmp/bv.sqlite3
python3 app.py export-history 1 --database /tmp/bv.sqlite3
```

- `observe` 执行一次观察并留存每页原始清单；加 `--max-pages` 可主动暂停，截断时返回下一对标记。`resume` 从标记另开批次，批次以 `resume_of` 关联，不合并为原子快照。
- `export-current` 导出该次观察下每个键的当前条目：普通版本进 current_files，删除标记进 current_delete_markers。
- `export-history` 导出观察到的全部历史版本及状态。`verified` 表示已按 sha256 核实且留存正文；`restore-pending` 是归档取回等待；`manifest-only` 是仅在清单发现、尚未取回；`received-unverified` 是收到正文但缺少校验证据；`missing` 仅在来源返回 404 时使用。

## 单独检查与收件

```sh
python3 app.py fixtures /tmp/bucket-versions-fixtures
python3 app.py inspect /tmp/bucket-versions-fixtures/sample.json
python3 app.py capture /tmp/bucket-versions-fixtures/sample.json --database /tmp/bucket-versions.sqlite3 --source local-partner
python3 app.py list --database /tmp/bucket-versions.sqlite3
```

生成夹具不会覆盖已有文件。运行错误以 JSON 输出到标准错误并返回退出码 2；原始输入无效时不生成成功收件。内部原件与任何对外展示副本应分开管理，不写入真实个人数据。
