# redprobe

Проба для измерений доступности от RedLine Studio. Ставится на любой Linux-сервер или домашний компьютер с Python 3.8+ и помогает [Red Atlas](https://atlas.redstudio.site) видеть, какие подсети хостеров открываются из разных сетей России. Одна строка установки, работает без root, портов не открывает.

## Что она делает

Раз в несколько минут проба спрашивает у сервера Атласа список заданий, выполняет их и отправляет результаты обратно по HTTPS. Задания такие:

- `atlas.ping`: проверить, отвечает ли адрес шлюза подсети хостера. Эхо-запрос, а если он недоступен без root, TCP-подключение к 443, 80 или 22;
- `atlas.tls1620`: скачать до 256 КБ по TLS с тестового файла хостера и записать, на каком объёме передача встала (так видно обрыв после 16–20 КБ);
- `rednode.tcp`: TCP-подключение и TLS-рукопожатие к серверу. Эти задания получают только пробы партнёров по договорённости с нами, обычная проба их не видит.

Чего она не делает: не открывает портов, не пропускает через себя чужой трафик и не ходит на запрещённые сайты. О компьютере проба отправляет только имя, регион и свою версию, которые вы указали при установке. Сервер видит её IP-адрес, по нему мы понимаем, из какой сети идёт проверка.

## Ограничения

- По умолчанию не больше 20 новых проверок в секунду (`REDPROBE_MAX_PPS`) и 16 одновременно (`REDPROBE_WORKERS`).
- Сервис systemd ограничен 96 МБ памяти и 20% одного ядра.
- Зависимостей нет, только стандартная библиотека Python.

## Установка

Нужно приглашение: одноразовый токен. Пока мы выдаём их вручную, напишите [@redlinefounder](https://t.me/redlinefounder) и укажите город и провайдера, через которого сервер выходит в интернет.

```
git clone https://github.com/REPLACE_ORG/redprobe && cd redprobe
sh install.sh https://atlas.redstudio.site <токен> <имя> <регион>
```

Скрипт кладёт `redprobe.py` в `~/.local/share/redprobe`, настройки в `~/.config/redprobe/redprobe.conf` (права 600), регистрирует пробу и запускает её пользовательским сервисом systemd. Токен одноразовый: после регистрации проба получает свой id и секрет, а токен из настроек стирается. Чтобы проба стартовала после перезагрузки без входа в систему, выполните `loginctl enable-linger $USER`. Если systemd недоступен, скрипт подскажет строку для cron.

Регион пишите латиницей, например `ru-msk`, `ru-spb`, `ru-ekb`. Регион, начинающийся с `control`, означает зарубежную контрольную точку.

## Настройки

Файл `KEY=VALUE` или переменные окружения с теми же именами:

| Ключ | По умолчанию | Что значит |
|---|---|---|
| `REDPROBE_SERVER` | | адрес сервера, `https://atlas.redstudio.site` |
| `REDPROBE_NAME` | имя хоста | как проба видна у нас |
| `REDPROBE_REGION` | | регион, см. выше |
| `REDPROBE_ENROLL_TOKEN` | | приглашение, нужно только для первого запуска |
| `REDPROBE_KINDS` | все | виды заданий через запятую |
| `REDPROBE_MAX_PPS` | 20 | сколько проверок начинать в секунду |
| `REDPROBE_WORKERS` | 16 | сколько проверок идёт одновременно |
| `REDPROBE_MAX_JOBS` | 500 | сколько заданий брать за раз |

Команды: `redprobe.py` работает постоянно, `redprobe.py --once` выполняет одну пачку заданий, `redprobe.py --register` только регистрирует пробу.

## Удаление

```
systemctl --user disable --now redprobe.service
rm -rf ~/.local/share/redprobe ~/.config/redprobe ~/.config/systemd/user/redprobe.service
```

## Протокол

JSON поверх HTTPS.

- `POST /probe/v1/register` с телом `{"enroll_token", "name", "region", "version", "kinds"}` отвечает `{"probe_id", "secret"}`.
- `GET /probe/v1/jobs?max=N` отвечает `{"jobs": [...], "next_in": секунды}`.
- `POST /probe/v1/results` с телом `{"results": [...]}` отвечает `{"stored": n}`.

После регистрации запросы подписаны: заголовки `X-Probe-Id`, `X-Probe-Ts` и `X-Probe-Sig` = hex HMAC-SHA256(secret, `"<id>|<ts>|<тело>"`). Подписи старше двух минут сервер отклоняет.

## In English

redprobe is a small outbound-only measurement probe for Red Atlas, a free live map of which hosting subnets are reachable from Russian networks. Python 3.8+, standard library only, no root, no open ports, rate-limited. Install with `sh install.sh https://atlas.redstudio.site <invite-token> <name> <region>`; ask [@redlinefounder](https://t.me/redlinefounder) for an invite.

## Лицензия

MIT, см. [LICENSE](LICENSE).
