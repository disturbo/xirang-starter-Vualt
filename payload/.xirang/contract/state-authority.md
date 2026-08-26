# StateStore 唯一权威与陈旧库隔离契约

## 唯一定位

生产读写必须先读取 `.xirang/local-config.json`，并同时核对 `workspace_root`、`workspace_id`、`runtime_dir`。唯一权威数据库固定为登记运行根下的 `state/state.sqlite3`，其 cutover sentinel 与 lock 必须由该数据库路径派生。

显式参数、`XIRANG_RUNTIME_DIR` 或 `XIRANG_STATE_DB` 只要与登记值不一致，就返回 `non_authoritative_database`，不得打开候选数据库，也不得把候选库中的“任务不存在”当成权威查询结果。

未配置工作区只保留首次迁移所需的兼容定位；一旦存在 schema v2 `local-config`，上述绑定失败关闭。

## 诊断

`.standards/xirang_state_cli.py authority-doctor` 是只读入口。它核对 local-config、workspace、runtime、数据库、cutover sentinel 与 lock，并扫描 Vault `.xirang` 下的 `state.sqlite3`。发现非权威库时返回 `non_authoritative_database` 和唯一权威路径，不修改现场。

## 隔离

`.standards/xirang_state_authority.py quarantine` 默认只生成计划。正式执行必须显式使用 `--apply`，并满足以下条件：

- 只接受当前 Vault 固定路径 `.xirang/contract/recovery-roots.yaml`；参数不得替换登记表；
- 候选库位于当前 Vault `.xirang` 且由 doctor 可证明不是权威库；
- 权威数据库永远禁止隔离；
- 同时取得权威库与候选库的 cutover 排他锁，并在锁内重新核对 binding、候选发现结果、文件族及 hash；
- 使用登记恢复根逐文件保存数据库、WAL、SHM、cutover sentinel 与 lock 的 pre-image；
- 先写 `prepared` 追加式审计，再移动到当次已选中的登记 objects 根隔离目录，最后写 `completed`；
- 任一移动或完成审计失败时，将已移动文件全部放回原位；
- 不删除数据库，不覆盖既有隔离目标，不接受临时选择的恢复目录。
