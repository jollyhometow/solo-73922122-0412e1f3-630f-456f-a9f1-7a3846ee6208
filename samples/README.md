# 样例脚本

先启动服务：`docker compose up --build`（api-a=8001，api-b=8002）。
脚本默认访问 `http://localhost:8001` 与 `http://localhost:8002`，
可用环境变量覆盖：`API_A=http://host:port API_B=http://host:port bash samples/01_out_of_order.sh`。

| 脚本 | 演示内容 |
|---|---|
| `01_out_of_order.sh` | **乱序到达**：子修订先到 → 观测 `pending`（待补齐、不出有效值）→ 父到达后自动重算为 `valid`；写 api-a、读 api-b，两进程结论一致 |
| `02_edit_delete_conflict.sh` | **改删并发**：一台设备改值、一台设备删除 → `conflict`（不输出单一有效观测）→ 人工裁决（以全部分支为父）→ 迟到的并发分支重新形成冲突 → 用过期分支裁决返回 **409** → 按当前分支重新裁决成功 |
| `03_idempotency_and_rejection.sh` | **幂等与整包拒绝**：同号同内容重传幂等；同号异内容 / 循环引用 / 跨观测引用（含父后到的反向情形）→ **422 整包拒绝、不留部分记录** |

依赖：`curl`；如有 `jq` 或 `python3` 会美化 JSON 输出，否则原样打印。
