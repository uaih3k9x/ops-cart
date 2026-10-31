# 备用远程通道（tailnet 不通时）

平时远程进推车走 tailnet。tailnet 的协调服务器连不上（比如被墙）时，用这条备用通道：
**推车主动** ssh 到一台跳板机，把自己的 22 口反向挂在跳板机的本机回环上；人先登上跳板机，再从那里进推车。

```
推车 ──ssh -R localhost:REMOTE_PORT──► 跳板机          人 ──ssh──► 跳板机 ──ssh -p REMOTE_PORT localhost──► 推车
```

- 默认**不启用**：没有 `/etc/ops-cart/revtunnel.env`，`cart-revtunnel.service` 不起。
- 跳板机上只在 localhost 听，外面直接扫跳板机扫不到推车；进推车照旧要推车自己的 ssh 认证。
- 推车只认事先核对过的跳板机指纹（`StrictHostKeyChecking=yes`），断了 20 秒后自动重连。
- 推车要能连出去（连上 SCNUNET 或 LAN2 有网）。跳板机要推车够得着、人也够得着：
  有公网 IP 的云主机，或者同在校园网里的实验室机器都行。

## 跳板机要满足

- 开着 sshd，推车能连到它的 ssh 口。
- 建一个专用账号（下面叫 `cart-tunnel`），**只许转发、不许登录 shell**。
- `AllowTcpForwarding` 没被关（默认是开的）；`GatewayPorts` 保持默认 `no`。

## 在推车上装（人在车边或已能登上推车时）

```bash
# 1. 生成推车专用钥匙 (只给这条通道用)
sudo ssh-keygen -t ed25519 -N '' -C ops-cart-revtunnel -f /etc/ops-cart/revtunnel_ed25519

# 2. 抓跳板机指纹, 和跳板机上 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` 的结果对一下, 一致才留
sudo sh -c 'ssh-keyscan -p <跳板机ssh口> -t ed25519 <跳板机地址> > /etc/ops-cart/revtunnel_known_hosts'
ssh-keygen -lf /etc/ops-cart/revtunnel_known_hosts

# 3. 配置 (照 examples/revtunnel.example.env 改)
sudo install -m 0600 examples/revtunnel.example.env /etc/ops-cart/revtunnel.env
sudo vi /etc/ops-cart/revtunnel.env

# 4. 装单元
sudo install -m 0644 deploy/systemd/cart-revtunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
```

## 在跳板机上（一次）

把推车的公钥（`/etc/ops-cart/revtunnel_ed25519.pub`）加到 `cart-tunnel` 的 `~/.ssh/authorized_keys`，**前面加限制**，
只许在 localhost 的那个口上反向监听：

```
restrict,port-forwarding,permitlisten="localhost:22022",command="/bin/false" ssh-ed25519 AAAA... ops-cart-revtunnel
```

（`22022` 换成 `REMOTE_PORT`。`permitlisten` 要 OpenSSH 7.8 以上。）

## 启用和验

```bash
sudo systemctl enable --now cart-revtunnel.service
systemctl status cart-revtunnel.service          # active (running)
journalctl -u cart-revtunnel -n 20               # 有 "remote port forwarding failed" 就是口被占了或跳板机限制没写对
```

在跳板机上：`ssh -p 22022 <推车账号>@localhost`，能进推车、`hostname` 是 `ops-cart` 就算通。
从自己电脑一步进：`ssh -J <跳板机> -p 22022 <推车账号>@localhost`。

## 停用

`sudo systemctl disable --now cart-revtunnel.service`；不想留痕就再删 `/etc/ops-cart/revtunnel*`，
并从跳板机的 `authorized_keys` 里删掉那一行（推车丢了也照这样做）。
