* [Portfly](#Portfly)
* [Usage](#Usage)
    * [Server](#Server)
    * [Client](#Client)
    * [Optional Parameters](#Optional-Parameters)

# Portfly

A pure Python lite implementation of SSH Port Forwarding, includes
remote port forwarding and local port forwarding, featured by
non-blocking socket and event IO.

Basicly, SSH remote port forwarding is a very cheap way to get `Intranet
Penetration`, and the forwarding path is secured! But there are a few
things you might not like:

* If you want to make the remote server listened at `0.0.0.0` other than
`127.0.0.1`, `GatewayPorts` option in /etc/ssh/sshd_config must be a yes.
You need sudo priviledge.
* End-to-end encryption may be already satisfied, and you definitely 
want to forward data as quick as possible. You don't need an extra level of
encryption.
* Tcp connection (ssh session) would be broken by any reason, and you
desperately need a reconnection mechanism, which is not provided by ssh.

If so, portfly kicks in!

![remote_port_forwarding](/remote_port_forwarding.png)

And, portfly also supports local port forwarding, which is usually
used to `directly get through the firewall`!


![local_port_forwarding](/local_port_forwarding.png)

# Usage

```shell
$ python portfly.py -h
```

## Server

You can arbitrarily specify a listen ip and port in server side.

``` shell
$ python portfly.py -s server_listen_ip:port
```

Server can be connected by multiple client, each client mapping port
has a Process in server.

## Client

The client command line configuration is just like ssh port
forwarding. The extra `+` can leave the whole parameters unquoted.

``` shell
$ python portfly.py -c [-L] mapping_port:target_ip:port+server_ip:port
```

Each client process can map only one port to server.

When `-L` is specified, it's in local port forwarding mode, so the
`target_ip:port` is in the viewpoint of server.

## Optional Parameters

`--log`, `INFO` or `DEBUG`, default is `WARNING`.

`-x`, to specify a very simple encryption in case you don't want to be naked
completely. Client side only.

`-L`, local port forwarding. Default is remote port forwarding. Client side
only.

Have Fun... ^____^

