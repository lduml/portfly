* [portfly](#portfly)
* [Usage](#Usage)
    * [Start Server](#Start-Server)
    * [Start Client](#Start-Client)
    * [Optional Parameters](#Optional-Parameters)

# portfly

A pure Python lite implementation of SSH Remote Port Forwarding, featured by
non-blocking socket and event IO.

Basicly, SSH remote port forwarding is a very cheap way to get `Intranet
Penetration`, and the forwarding path is secured! But there are a few
things you might not like:

* If you want to make the remote server listened at `0.0.0.0` other than
`127.0.0.1`, `GatewayPorts` option in /etc/ssh/sshd_config must be a yes.
You need sudo priviledge.
* End-to-end encryption may be already satisfied, and you definitely 
want to forward as quick as possible, so you don't need an extra level of
encryption.
* Tcp connection would be broken by any reason, and you desperately need
reconnection mechanism, which is not provided by ssh.

So, portfly kicks in!

# Usage

## Start Server

``` shell
$ python portfly.py -s server_listen_ip:port
```

## Start Client

``` shell
$ python portfly.py -c mapping_port:target_ip:port+server_ip:port
```

The client cmd configuration is just like ssh. The extra `+` used above
can leave the whole parameters unquoted.

## Optional Parameters

`--log`, `INFO` or `DEBUG`, default is `WARNING`.

`-x`, to specify a very simple encryption in case you don't want to be naked
completely.

Have Fun... ^____^
