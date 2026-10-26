# 部署说明

目标服务器：`root@10.0.0.66`（Ubuntu 22.04，内网直连 22 端口；
外网入口 `quant.example.com:5029` 经网关 nginx 转发，链路偶发断连，仅作备用），
部署目录 `/opt/quant-trading`。

## 一键部署

```bash
./deploy.sh            # 首次：代码 + data/ + planwatch 状态 + 安装 systemd 服务 + 重启
```

浏览器打开 `http://<服务器IP>:8765/`，**首次访问注册账户**（本地 `data/users/`
已随 rsync 同步，原有账户可直接登录）。

## 日常

```bash
./deploy.sh code       # 改完代码：同步 + 重启（约 1~2 分钟）
./deploy.sh data       # 只推行情数据
./deploy.sh status     # 服务状态 + HTTP 健康
./deploy.sh logs               # 跟仪表盘日志
./deploy.sh logs planwatch     # 跟盯盘日志
```

## 服务构成（systemd）

| 服务 | 进程 | 说明 |
|---|---|---|
| `quant-analyzer` | `python -m analyzer serve --host 0.0.0.0 --port 8765` | Web 仪表盘；内置交易日 15:30（CST）盘后全量更新调度器，**无需额外 cron**；分钟线后台同步、模型信号站内信推送都由它完成 |
| `quant-planwatch` | `python -m planwatch monitor` | 交易计划盯盘：交易时段 5s 轮询，价格位/止损/T 信号提醒写入统一站内信（Web 🔔 实时可见）；非交易时段 30s 空转 |

两个服务均 `Restart=on-failure`，日志在 `journalctl -u <服务名>`。

## 环境变量

`/opt/quant-trading/.env`（首次部署自动生成，`chmod 600`）：

- `AI_API_KEY` 等：AI 综合分析，留空即停用该功能；
- `CLASH_API=127.0.0.1:9090`：限流时自动轮换服务器本地 Clash 出口节点；
- `SMTP_*` / `MAIL_TO` 等：昨夜美股简报邮件推送（渠道同 iam 服务器
  github-trending-push 项目的 email 通道，QQ 邮箱 SMTP）；留空 `SMTP_USER`/
  `SMTP_PASS`/`MAIL_TO` 即停用；每工作日 08:40 CST 推送，详见仓库 README
  「昨夜美股简报邮件推送」一节。

本地在仓库根放一份 `deploy.env`（已 gitignore）即可随部署同步，
改完 `./deploy.sh code` 或 `systemctl restart` 生效。

## 安全提示

- 8765 直绑 0.0.0.0，靠系统自带多用户登录（scrypt 密码哈希、HttpOnly 会话）
  鉴权。内网 `http://10.0.0.66:8765/` 直接可达；**外网访问需在网关
  （203.0.113.10 / nginx）上再加一条 8765 转发**（当前未配置，公网 8765 返回 502）。
- 行情数据文件全部只读共享，账户间自选/流水/站内信隔离。
- 服务器对连续快速 SSH 建连偶发 reset，deploy.sh 已内置自动重试。
- 如需 HTTPS/域名，再在前面加 nginx 反代即可。

## 回滚 / 排障

```bash
ssh -p 5029 root@quant.example.com
systemctl status quant-analyzer quant-planwatch
journalctl -u quant-analyzer --since "10 min ago"
# 手动试跑盘后更新逻辑（前台，Ctrl+C 退出）：
cd /opt/quant-trading && .venv/bin/python -m downloader all
```

- `failed_*.txt` 很长 = 数据源限流，降 `--rate` 等几分钟后重跑同一命令即可；
- 服务器磁盘注意：全市场日线 + 分钟线长期累积约数 GB，`df -h /opt` 定期检查。
