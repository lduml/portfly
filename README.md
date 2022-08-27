# report

A pure python `lite` version of SSH Remote Port Forwarding.

SSH remote port forwarding basicly is the simplest way to get `Intranet
Penetration`, and the forwarding path is secured. If you want to make the
remote port listened at 0.0.0.0 other than 127.0.0.1, GatewayPorts option
in /etc/ssh/sshd_config must be yes.

However, end-to-end encryption may be already satisfied, and you want to
forward as fast as possible, so you don't need a secured forwarding path, 
which is a waste. And if you don't want to or cannot change the
configuration of sshd, but you still need the remote port to be listened at
0.0.0.0. For both cases above, `report` kicks in.

Normally, you should run server in backgroupd, like:

``` shell
$ nohup python3 report_server.py <server_listen_port> >> server.log &
```

The client comand line just mimics ssh remote port forwarding:

``` shell
$ python3 report_client.py [-x] public_port:target_host:target_port server_ip:server_port
```

`-x` is a very simple encryption option in case you don't want to be naked
completely.

Have Fun... ^____^
