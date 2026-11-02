# 部署与运维

> 验证环境：Alibaba Cloud Linux 4 · nginx 1.30 · Python 3.11
> 文中 `<server-ip>` 请替换为你的服务器地址。

## 一、部署形态

```
公网 80/443
    │
    ▼
nginx（反向代理，/etc/nginx/conf.d/exam-scheduler.conf）
    │
    ▼
127.0.0.1:8765  exam-scheduler.service（后端，只监听回环）
                        │
                        ▼ HTTP 拉取
                 127.0.0.1:9000  exam-queue-mock.service（模拟叫号数据源）
```

两个后端都只监听 `127.0.0.1`，外部无法直连，必须经过 nginx。

| 项目 | 路径 |
|---|---|
| 代码 | `/opt/exam-scheduler` |
| nginx 配置 | `/etc/nginx/conf.d/exam-scheduler.conf` |
| systemd 单元 | `/etc/systemd/system/exam-scheduler.service`、`exam-queue-mock.service` |
| 自签证书 | `/etc/ssl/exam-scheduler/{cert,key}.pem` |
| 服务账号 | `examsvc`（nologin，无 shell） |

---

## 二、从零部署

### 1. 准备账号与目录

```bash
useradd -r -s /sbin/nologin examsvc 2>/dev/null || true
mkdir -p /opt/exam-scheduler
chown -R examsvc:examsvc /opt/exam-scheduler
```

### 2. 放代码

```bash
git clone <repo-url> /opt/exam-scheduler
# 或者本地 rsync/scp 上传
chown -R examsvc:examsvc /opt/exam-scheduler
```

项目零依赖，不需要 `pip install`。

### 3. 安装 systemd 单元

```bash
cp /opt/exam-scheduler/deploy/exam-scheduler.service   /etc/systemd/system/
cp /opt/exam-scheduler/deploy/exam-queue-mock.service  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now exam-queue-mock exam-scheduler
systemctl status exam-scheduler exam-queue-mock
```

### 4. 配置 nginx

```bash
# 自签证书（裸 IP 无法签发受信任证书）
mkdir -p /etc/ssl/exam-scheduler
openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout /etc/ssl/exam-scheduler/key.pem \
  -out /etc/ssl/exam-scheduler/cert.pem \
  -subj "/C=CN/O=ExamScheduler/CN=<server-ip>" \
  -addext "subjectAltName=IP:<server-ip>"
chmod 600 /etc/ssl/exam-scheduler/key.pem

cp /opt/exam-scheduler/deploy/nginx-exam-scheduler.conf /etc/nginx/conf.d/
nginx -t && systemctl reload nginx
```

### 5. 验收

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/
curl -s -X POST http://127.0.0.1:8765/api/recommend \
  -H "Content-Type: application/json" \
  -d '{"codes":["CBC","ECG","CXR"],"fasting":true}'
```

---

## 三、常用命令

```bash
systemctl status exam-scheduler exam-queue-mock
systemctl restart exam-scheduler        # 改了后端代码
journalctl -u exam-scheduler -f         # 看日志
journalctl -u exam-queue-mock -n 50
nginx -t && systemctl reload nginx      # 改了 nginx 配置
```

---

## 四、更新代码

服务器上就是普通 git 仓库，拉取即可：

```bash
cd /opt/exam-scheduler
git pull
systemctl restart exam-scheduler
```

改了哪部分就重启哪个服务：

| 改动位置 | 需要重启 |
|---|---|
| `backend/app/rules.py` | `exam-scheduler` |
| `backend/app/*.py` | `exam-scheduler` |
| `simulator/queue_mock.py` | `exam-queue-mock` |
| `frontend/*` | 不用重启，浏览器刷新即可 |

没有 git 时直接覆盖：

```bash
scp -r exam_scheduler/{backend,frontend,simulator} root@<server-ip>:/opt/exam-scheduler/
ssh root@<server-ip> "chown -R examsvc:examsvc /opt/exam-scheduler && systemctl restart exam-scheduler"
```

---

## 五、证书

默认是**自签证书**（CN/SAN 为服务器 IP），浏览器会提示"不安全"。原因是裸 IP 无法签发受信任证书——Let's Encrypt 等 CA 需要域名做校验。

有域名后（`exam.example.com` 解析到本机）：

```bash
dnf install -y certbot python3-certbot-nginx
certbot --nginx -d exam.example.com
```

certbot 会自动改写配置并续期。也可手动把 `ssl_certificate` 两行指向 certbot 生成的路径。

---

## 六、加访问密码（建议）

默认是公开可访问的，任何人打开 IP 都能用。要挡一下：

```bash
# 1) 生成密码文件
printf "demo:%s\n" "$(openssl passwd -apr1 'YourPassword')" > /etc/nginx/.htpasswd
chmod 640 /etc/nginx/.htpasswd

# 2) 在两个 location / 里各加两行
#    auth_basic "Restricted";
#    auth_basic_user_file /etc/nginx/.htpasswd;

nginx -t && systemctl reload nginx
```

---

## 七、切换真实数据源

目前跑的是模拟数据（`exam-queue-mock`）。接医院接口时：

```bash
systemctl edit exam-scheduler
# [Service]
# ExecStart=
# ExecStart=/usr/bin/python3 -m backend.app.server --port 8765 --queue-api http://医院地址:端口

systemctl daemon-reload && systemctl restart exam-scheduler
systemctl disable --now exam-queue-mock    # 不再需要模拟数据
```

数据源只需返回 `GET /queue/snapshot`，字段映射见 `backend/app/data_source.py`。拉取失败会自动降级到上一次成功的快照，不会阻断推荐。

---

## 八、调整叫号节奏（演示用）

模拟数据源默认**所有科室从 1 号起叫，每 60 秒往后走一位**，等待时长仍按 `人数 × 人均服务时长` 真实折算。改节奏不用动代码：

```bash
systemctl edit exam-queue-mock
# [Service]
# ExecStart=
# ExecStart=/usr/bin/python3 -m simulator.queue_mock --host 127.0.0.1 --port 9000 --call-interval 20

systemctl daemon-reload && systemctl restart exam-queue-mock
```

| 参数 | 作用 | 默认 |
|---|---|---|
| `--call-interval` | 每隔多少秒叫下一位 | 60 |
| `--serving-start` | 起始叫号（模拟"已经叫过一批"） | 1 |
| `--idle-reset` | 多久无人访问就把号归零，0 表示关闭 | 120 |

演示中途想把号归零，不用重启：

```bash
curl -X POST http://127.0.0.1:9000/queue/reset
```

模拟源首页的看板底部也有「重置叫号」按钮。默认不对公网暴露，需要时可用
`ssh -L 9000:127.0.0.1:9000 <user>@<server-ip>` 做本地端口转发。
