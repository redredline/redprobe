<img src="assets/red-atlas.svg" width="48" height="48" alt="Red Atlas">

# redprobe

Проба для [Red Atlas](https://atlas.redstudio.site) от RedLine Studio. [English version](#english)

Red Atlas показывает, какие подсети хостеров сейчас открываются из России, и помогает отличить блокировку от упавшего сервера. Сейчас он смотрит через публичные сети измерений RIPE Atlas и Globalping, и каждая подсеть проверяется примерно раз в сутки-двое. Проба, которую вы ставите у себя в российской сети, проверяет сотни подсетей каждые пять минут и может поймать то, чего публичные сети не видят, например обрыв соединения после первых 16-20 КБ.

## Что проба делает

Примерно раз в пять минут проба забирает у сервера Атласа пачку заданий, выполняет их и по HTTPS отправляет результаты обратно. Задания у неё трёх видов.

`atlas.ping` проверяет, отвечает ли один адрес в подсети /24 хостера, обычно шлюз .1 или .254. На этот же адрес смотрит карта Атласа. Если система разрешает пинг без root (настройка `net.ipv4.ping_group_range`), уходит один ICMP-запрос, иначе проба открывает TCP-соединение на порт 443, 80 или 22 и сразу его закрывает. Многие шлюзы отвечают только на пинг, а по TCP молчат, даже когда открыты, поэтому ответ «не отвечает» от пробы, которая работает через TCP, Атлас не учитывает. Как включить пинг, написано в разделе «Как подключиться». У хостеров, которые раздают клиентам всю /24 целиком, такой адрес может оказаться сервером клиента хостера.


`atlas.tls1620` скачивает по TLS первые 256 КБ открытого тестового файла хостера (сейчас это файлы для проверки скорости у Hetzner, OVH и Vultr) и записывает, на каком объёме передача встала. Так видно, режет ли сеть соединение после первых 16-20 КБ. На сервере Атласа эта проверка пока выключена.

В ICMP-пакете проба пишет слово `redprobe`, а в запросе к тестовому файлу представляется как `redprobe/0.3.1`, так что хостер может понять, кто к нему пришёл.

## Чего проба не делает

Проба не открывает портов и ничего не слушает: все соединения она начинает сама. Root ей не нужен, из зависимостей только Python 3.8 или новее. Через неё не идёт чужой трафик, она не читает ваш трафик и не отправляет его содержимое. Среди адресов, к которым она обращается, бывают заблокированные в России, ради их поиска пробу и ставят. Страниц проба не открывает и ничего не скачивает, кроме тестовых файлов хостеров.

Задания других видов проба пропускает, даже если сервер их пришлёт: выполняются только виды из настройки `REDPROBE_KINDS`, по умолчанию там `atlas.ping`, `atlas.tls` и `atlas.tls1620`. Весь код лежит в одном файле `redprobe.py` на 934 строки.

## Сколько ресурсов уходит

За одну пачку проба просит у сервера до 500 заданий (`REDPROBE_MAX_JOBS`), начинает не больше 20 проверок в секунду (`REDPROBE_MAX_PPS`) и ведёт не больше 16 одновременно (`REDPROBE_WORKERS`). Пачка из 500 адресов обычно занимает от 25 до 45 секунд, потом проба ждёт пять минут, столько просит сервер. Сервис systemd ограничен 96 МБ памяти и 20 % одного ядра. В нашем тесте проба занимала около 13 МБ памяти и тратила примерно 0,3 секунды процессора на пачку.

Трафик мы посчитали по размеру запросов: на пачку уходит около 85 КБ JSON с заданиями и результатами плюс от 100 до 500 байт на каждую проверку. В сутки получается примерно 40 МБ, если работает ICMP, и до 80 МБ через TCP, то есть 1-2,5 ГБ в месяц. Проверка `atlas.tls1620`, когда её включат, добавит до 1 МБ на пачку, это ещё около 270 МБ в сутки и 8 ГБ в месяц. Если трафик у вас считают, отключите её (как это сделать, написано в разделе «Настройки»).

## Приватность

При регистрации проба отправляет имя и регион, которые вы указали при установке, свою версию и список видов заданий. Потом на сервер уходят только результаты: ответил ли адрес, время ответа и способ проверки (ICMP или TCP с номером порта), а для теста 16-20 ещё число полученных байт, признак того, что передача встала, и текст ошибки, если она была. Ничего о вашем компьютере и вашей сети проба не собирает. Соединение идёт по HTTPS с проверкой сертификата, запросы подписаны секретом пробы (HMAC-SHA256).

Проба ходит на два адреса. Сначала на ретранслятор `https://probe.redstudio.site:8444` на нашем сервере в Москве, потому что у части российских домашних провайдеров сервер Атласа за границей не открывается. Ретранслятор пропускает только запросы пробы, передаёт их серверу Атласа вместе с IP-адресом пробы и журналов не ведёт. Если он не отвечает, проба идёт напрямую на `https://atlas.redstudio.site`.

IP-адрес пробы сервер Атласа видит, как любой сайт видит адрес посетителя. При регистрации сервер один раз определяет по нему сеть: отправляет адрес в открытую базу [RIPEstat](https://stat.ripe.net) (сервис RIPE NCC) и сохраняет только номер AS, имя сети, страну и примерный город. Сам адрес в базу Атласа не записывается, журналов запросов на сервере нет. Если адрес не в России, регистрация не проходит: точки за пределами России мы подключаем только по договорённости. Свою автономную систему проба не проверяет, поэтому VPS у российского хостера не мерит сети этого хостера изнутри.

Токен из бота привязан к вашему аккаунту Telegram. Вместе с пробой мы храним id этого аккаунта, тип подключения и город, которые вы указали в боте, чтобы бот мог показать ваши точки и написать, если с ними что-то не так. На страницах подсетей и в API проверка подписана случайным id пробы вида `probe:p1a2b3c4d5e6`, без имени, региона, аккаунта и IP.

### Red Node и адреса клиентов

У RedLine Studio есть второй сервис, Red Node: он следит, открываются ли из России серверы клиентов. Протокол у проб общий, поэтому в коде есть вид заданий `rednode.tcp` (TCP-подключение и TLS-рукопожатие к одному серверу). Публичные пробы волонтёров его не получают. Им сервер раздаёт только задания Атласа, то есть проверки подсетей хостеров, которые и так публичны в BGP, а адреса клиентов Red Node из этих заданий исключены. Задания Red Node выполняют только наши серверы и партнёры, которых мы знаем лично. Адреса клиентов на пробы волонтёров никогда не отправляются.

Помогать Red Node волонтёры всё равно будут, только косвенно: мы сами сопоставляем проверенные подсети с подсетями, где стоят серверы клиентов. Проба об этом ничего не знает и ничего лишнего не получает.

## Как подключиться

Подойдёт Linux-сервер или домашний компьютер с Linux, где есть Python 3.8 или новее и systemd. Точки мы подключаем в российских сетях: домашний и мобильный интернет, российские VPS.

1. Получите токен в боте [@redatlas_robot](https://t.me/redatlas_robot). Он спросит, откуда проба будет выходить в интернет (домашний, мобильный интернет или VPS в России) и в каком городе она стоит, и пришлёт готовую команду установки с токеном. Токен одноразовый и действует сутки, на один аккаунт Telegram можно подключить две точки.

2. Включите пинг без root. Это единственная команда, которой нужен sudo:

   ```sh
   sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
   ```

   Настройка действует до перезагрузки. Чтобы она осталась навсегда:

   ```sh
   echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/99-redprobe.conf
   ```

   Без этой настройки проба проверяет адреса через TCP. Её «отвечает» мы учитываем, а «не отвечает» нет, потому что многие шлюзы по TCP молчат, даже когда открыты. Пробу после этой команды перезапускать не нужно, следующая пачка пойдёт пингом.

3. Поставьте пробу от обычного пользователя:

   ```sh
   git clone https://github.com/redredline/redprobe
   cd redprobe
   sh install.sh https://probe.redstudio.site:8444 ТОКЕН ИМЯ РЕГИОН
   ```

   Бот подставляет токен, имя и регион сам. Имя видим только мы, например `home-msk`. Регион пишите латиницей: `ru-msk`, `ru-spb`, `ru-ekb`. Регион, который начинается с `control`, означает зарубежную контрольную точку. Такие пробы могут получать другие задания (поиск шлюзов новых подсетей), поэтому ставьте его только по договорённости с нами.

   Скрипт кладёт `redprobe.py` в `~/.local/share/redprobe`, настройки в `~/.config/redprobe/redprobe.conf` с правами 600, регистрирует пробу и запускает её пользовательским сервисом systemd. Ещё он включает для вашего пользователя linger (`loginctl enable-linger`), иначе systemd остановит пробу, как только вы выйдете из системы, и не запустит после перезагрузки. После регистрации токен стирается из настроек, а проба получает свой id и секрет. Если сервер отказал в регистрации, скрипт напечатает причину и остановится. Если systemd для пользователя недоступен, например в контейнере, скрипт напечатает команду запуска и строку для `crontab -e`.

4. Проверьте, что проба работает:

   ```sh
   systemctl --user status redprobe
   journalctl --user -u redprobe -f
   ```

   После каждой пачки в журнале появляется строка `batch:` с числом заданий, числом ответивших адресов и временем пачки. Бот тоже напишет, когда точка подключится, а в разделе «Мои точки» покажет, в сети ли она и сколько проверок сделала за день.

Остановить пробу на время можно командой `systemctl --user stop redprobe`, запустить снова `systemctl --user start redprobe`.

### Обновление

Проба сама не обновляется. Чтобы поставить новую версию, скачайте файл и перезапустите сервис (настройки, id и секрет остаются прежними):

```sh
f=$HOME/.local/share/redprobe/redprobe.py; curl -fsSL https://raw.githubusercontent.com/redredline/redprobe/v0.3.1/redprobe.py -o $f.new && echo "a5d65d6bf9325ddfc3768b43f9350f7768b93e5f93378a7c8ce5856189ebc1a9  $f.new" | sha256sum -c - && mv $f.new $f && systemctl --user restart redprobe
```

Команда проверяет контрольную сумму файла и заменяет пробу только при совпадении. Если вы ставили пробу через `git clone`, можно и так: `git pull` в папке репозитория, затем `cp redprobe.py ~/.local/share/redprobe/` и `systemctl --user restart redprobe`.

### Удаление

```sh
systemctl --user disable --now redprobe
rm -rf ~/.local/share/redprobe ~/.config/redprobe ~/.config/systemd/user/redprobe.service
systemctl --user daemon-reload
```

Если linger не нужен другим вашим сервисам, выключите его командой `loginctl disable-linger`. На сервере Атласа точку можно отключить в боте [@redatlas_robot](https://t.me/redatlas_robot), в разделе «Мои точки».

## Настройки

Настройки лежат в `~/.config/redprobe/redprobe.conf` в виде `КЛЮЧ=значение`, те же ключи можно передать переменными окружения. После правки файла перезапустите пробу: `systemctl --user restart redprobe`.

- `REDPROBE_SERVER` задаёт адрес сервера, можно несколько через запятую. По умолчанию это ретранслятор `https://probe.redstudio.site:8444`, а если он не отвечает, `https://atlas.redstudio.site`. Если указан один из этих двух адресов, второй проба добавляет запасным сама.
- `REDPROBE_NAME` задаёт имя пробы, его видим только мы. По умолчанию берётся имя компьютера.
- `REDPROBE_REGION` задаёт регион, например `ru-msk`.
- `REDPROBE_ENROLL_TOKEN` хранит токен до регистрации, потом проба его стирает.
- `REDPROBE_KINDS` перечисляет через запятую виды заданий, которые проба выполняет, по умолчанию `atlas.ping,atlas.tls,atlas.tls1620`. `REDPROBE_PROXY` и остальные `REDPROBE_PROXY_*` нужны только нашим мобильным точкам за прокси, волонтёрам они не нужны.
- `REDPROBE_MAX_PPS` ограничивает, сколько проверок проба начинает за секунду, по умолчанию 20.
- `REDPROBE_WORKERS` ограничивает, сколько проверок идёт одновременно, по умолчанию 16.
- `REDPROBE_MAX_JOBS` задаёт, сколько заданий проба просит на одну пачку, по умолчанию 500.

Путь к файлу настроек меняется переменной `REDPROBE_CONF`.

Виды заданий сервер запоминает при регистрации. Чтобы с самого начала отказаться от теста 16-20 и его трафика, ставьте пробу так:

```sh
REDPROBE_KINDS=atlas.ping sh install.sh https://probe.redstudio.site:8444 ТОКЕН ИМЯ РЕГИОН
```

Если убрать вид из `REDPROBE_KINDS` уже после регистрации, сервер продолжит присылать такие задания, а проба будет их пропускать и писать об этом в журнал.

Проба запускается и вручную: `python3 ~/.local/share/redprobe/redprobe.py --once` выполняет одну пачку и выходит, без ключа работает постоянно, а `--register` только регистрирует пробу.

## Как используются результаты

Результаты пингов записываются в историю проверок подсети в Атласе, рядом с проверками RIPE Atlas и Globalping. Откуда берётся список подсетей, как подсеть получает цвет и сколько точек нужно для статуса «Заблокирована в России», написано в [методике](https://atlas.redstudio.site/method/). Все данные Атласа можно бесплатно забрать через [открытое API](https://atlas.redstudio.site/api/).

## Вопросы

**Это законно?** Проба делает то же, что команды `ping` и `curl`: обращается к публичным адресам хостеров и скачивает их открытые тестовые файлы. Она ничего не обходит, не пропускает через себя чужой трафик и не открывает сайтов. Похожие измерения в России ведут сотни проб RIPE Atlas. Мы не юристы и гарантий дать не можем, поэтому если сомневаетесь, лучше не ставьте пробу.

**Увидит ли кто-то мой IP?** Сервер Атласа видит его при каждом запросе, но не сохраняет. При регистрации адрес один раз уходит в RIPEstat, чтобы определить сеть и страну. Хостеры, чьи адреса проверяет проба, видят ваш IP как источник пинга или соединения, так бывает при любом обращении к их адресам. На сайте Атласа и в API ваш IP и имя пробы не показываются.

**Может прийти жалоба на сканирование?** Может. Проба за несколько минут обращается к сотням адресов в разных сетях, и системы защиты некоторых хостеров считают такое сканированием. Мы держим скорость низкой и проверяем по одному адресу на подсеть, но жалобу на ваш IP исключить нельзя. Если ставите пробу на арендованный сервер, загляните в правила своего хостера. Скорость можно снизить через `REDPROBE_MAX_PPS` и `REDPROBE_MAX_JOBS`.

**Сколько трафика?** Примерно 40-80 МБ в сутки. Когда на сервере включат тест 16-20, добавится около 270 МБ в сутки, если вы его не отключили.

**Что, если сервер Атласа взломают?** Проба выполнит только задания своих видов: пинг одного адреса, TCP-подключение к портам 443, 80 и 22 и загрузку до 256 КБ по TLS. Взломанный сервер сможет направить эти проверки на другие адреса, но запустить через пробу команды на вашем компьютере или открыть порт у него не получится. Темп проверок при этом всё равно ограничен `REDPROBE_MAX_PPS`.

## Протокол

JSON поверх HTTPS:

- `POST /probe/v1/register` с телом `{"enroll_token", "name", "region", "version", "kinds"}` отвечает `{"probe_id", "secret"}`;
- `GET /probe/v1/jobs?max=N` отвечает `{"jobs": [...], "next_in": секунды}`;
- `POST /probe/v1/results` с телом `{"results": [...]}` отвечает `{"stored": n}`.

После регистрации запросы подписаны: заголовки `X-Probe-Id`, `X-Probe-Ts` и `X-Probe-Sig`, где подпись равна hex HMAC-SHA256(secret, `"<id>|<ts>|<тело>"`). Подписи старше двух минут сервер отклоняет.

## Лицензия

MIT, см. [LICENSE](LICENSE). Copyright (c) 2026 RedLine Studio.

---

## English

redprobe is a measurement probe for [Red Atlas](https://atlas.redstudio.site) by RedLine Studio. Red Atlas shows which hosting subnets are reachable from Russia right now and helps tell a block from a dead server. Today it looks through the public measurement networks RIPE Atlas and Globalping, so each subnet gets checked about once every one or two days. A probe you run inside a Russian network checks hundreds of subnets every five minutes and can catch what public networks miss, such as connections that freeze after the first 16-20 KB.

### What the probe does

About every five minutes the probe fetches a batch of jobs from the Atlas server, runs them and sends the results back over HTTPS. There are three kinds of jobs.

`atlas.ping` checks whether one address in a hoster's /24 answers, usually the gateway .1 or .254, the same address the Atlas map looks at. If the system allows unprivileged ping (`net.ipv4.ping_group_range`), the probe sends one ICMP echo; otherwise it opens a TCP connection to port 443, 80 or 22 and closes it at once. Many gateways answer ping only and stay silent over TCP even when reachable, so Atlas ignores "no answer" from a probe that works over TCP; how to enable ping is under How to join. At hosters that hand a whole /24 to customers, that address may belong to a customer's server.

`atlas.tls` checks an address the way a client connects to it: it opens a TCP connection and then, depending on the service, makes a TLS handshake without sending data or starts the SSH key exchange and disconnects cleanly after the server's reply. That is 2-4 live addresses per /24, one connection per address per round, at most 20 connections a second. A ping is then only a hint that the address is alive and never makes a subnet open. One such check costs 1 to 9 KB of traffic (TCP about 1 KB, TLS about 6 KB, SSH about 9 KB). The server currently sends jobs of this kind only to our own points, and to volunteers' probes only by agreement.

`atlas.tls1620` downloads the first 256 KB of a hoster's public speed test file over TLS (currently Hetzner, OVH and Vultr) and records where the transfer stalled, which shows whether the network cuts connections after the first 16-20 KB. This check is switched off on the Atlas server for now.

ICMP packets carry the word `redprobe`, and requests for test files identify themselves as `redprobe/0.3.1`, so a hoster can tell where they come from.

### What it does not do

The probe opens no ports and listens on nothing; it only makes outbound connections. It needs no root and nothing beyond Python 3.8 or newer. It does not relay anyone's traffic, does not read your traffic and sends none of its content. Some of the addresses it contacts are blocked in Russia, which is what the measurements are meant to find. It opens no web pages and downloads nothing except the hosters' test files.

The probe skips jobs of any other kind even if the server sends them: it runs only the kinds listed in `REDPROBE_KINDS`, which defaults to `atlas.ping`, `atlas.tls` and `atlas.tls1620`. All the code is one 934-line file, `redprobe.py`.

### Resources

For each batch the probe asks the server for up to 500 jobs (`REDPROBE_MAX_JOBS`). The probe starts at most 20 checks per second (`REDPROBE_MAX_PPS`) and runs at most 16 at a time (`REDPROBE_WORKERS`). A batch of 500 addresses usually takes 25 to 45 seconds, then the probe waits five minutes, as the server asks. The systemd service is capped at 96 MB of memory and 20% of one core. In our test the probe used about 13 MB of memory and about 0.3 seconds of CPU per batch.

We estimated traffic from request sizes: about 85 KB of JSON per batch plus 100 to 500 bytes per check, roughly 40 MB a day with ICMP and up to 80 MB a day over TCP, or 1 to 2.5 GB a month. Once `atlas.tls1620` is switched on it adds up to 1 MB per batch, about 270 MB a day or 8 GB a month. If your traffic is metered, turn it off as shown under Settings.

### Privacy

At registration the probe sends the name and region you gave at install, its version and the list of job kinds. After that it sends only results: whether the address answered, the response time and the method (ICMP, or TCP with the port), plus for the 16-20 test the byte count, whether the transfer stalled and the error text if there was one. It collects nothing about your computer or your network. Connections use HTTPS with certificate checks, and requests are signed with the probe's secret (HMAC-SHA256).

The probe knows two addresses. It tries the relay `https://probe.redstudio.site:8444` on our server in Moscow first, because some Russian home ISPs cannot reach the Atlas server abroad. The relay passes only probe requests, forwards them to the Atlas server together with the probe's IP address and keeps no logs. If the relay does not answer, the probe goes straight to `https://atlas.redstudio.site`.

The Atlas server sees the probe's IP address the way any website sees a visitor's address. At registration it looks the network up once: it sends the address to the public [RIPEstat](https://stat.ripe.net) database (run by RIPE NCC) and keeps only the AS number, the network name, the country and an approximate city. The address itself is not written to the Atlas database, and the server keeps no request logs. If the address is not in Russia, registration fails: probes outside Russia join only by arrangement with us. A probe never checks its own autonomous system, so a VPS at a Russian hoster does not measure that hoster's networks from the inside.

A token from the bot is tied to your Telegram account. With the probe we keep that account's id and the connection type and city you gave the bot, so the bot can list your probes and tell you when something is wrong. On subnet pages and in the API a check is labelled with the probe's random id, such as `probe:p1a2b3c4d5e6`, with no name, region, account or IP.

#### Red Node and customer addresses

RedLine Studio runs a second service, Red Node, which watches whether customers' servers are reachable from Russia. The probe protocol is shared, so the code has a `rednode.tcp` job kind (a TCP connect and TLS handshake to one server). Public volunteer probes never get it. The server gives them Atlas jobs only, checks of hoster subnets that are public in BGP anyway, with Red Node customers' addresses excluded. Red Node jobs run only on our own servers and on partners we know personally, and customer addresses are never sent to volunteer probes.

Volunteers still help Red Node indirectly: we match the measured subnets against the subnets where customers' servers live ourselves. The probe knows nothing about it and receives nothing extra.

### How to join

You need a Linux server or a home computer running Linux with Python 3.8 or newer and systemd. We connect probes inside Russian networks: home broadband, mobile internet, Russian VPS.

1. Get a token from the bot [@redatlas_robot](https://t.me/redatlas_robot) (in Russian). It asks where the probe goes online (home broadband, mobile internet or a VPS in Russia) and in which city, then sends a ready install command with the token. A token works once and expires after 24 hours; one Telegram account can run two probes.

2. Enable unprivileged ping. This is the only command that needs sudo:

   ```sh
   sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
   ```

   The setting lasts until reboot. To keep it:

   ```sh
   echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/99-redprobe.conf
   ```

   Without it the probe checks addresses over TCP. We count its "answers" but ignore its "no answer", because many gateways stay silent over TCP even when reachable. The probe needs no restart after the command; its next batch goes over ping.

3. Install as an ordinary user:

   ```sh
   git clone https://github.com/redredline/redprobe
   cd redprobe
   sh install.sh https://probe.redstudio.site:8444 TOKEN NAME REGION
   ```

   The bot fills in the token, name and region for you. Only we see the name, for example `home-msk`. Write the region in Latin letters: `ru-msk`, `ru-spb`, `ru-ekb`. A region starting with `control` marks a foreign control point. Such probes may get other jobs (finding gateways of new subnets), so use it only after agreeing with us.

   The script puts `redprobe.py` into `~/.local/share/redprobe` and the settings into `~/.config/redprobe/redprobe.conf` (mode 600), registers the probe and starts it as a systemd user service. It also enables lingering for your user (`loginctl enable-linger`); without it systemd stops the probe when you log out and does not start it after a reboot. After registration the token is removed from the settings and the probe gets its own id and secret. If the server refuses the registration, the script prints the reason and stops. If systemd user services are unavailable, for example in a container, the script prints a start command and a line for `crontab -e`.

4. Check that it runs:

   ```sh
   systemctl --user status redprobe
   journalctl --user -u redprobe -f
   ```

   After each batch the log gets a `batch:` line with the number of jobs, the number of addresses that answered and the time the batch took. The bot also tells you when the probe connects, and «Мои точки» there shows whether it is online and how many checks it ran today.

To pause the probe run `systemctl --user stop redprobe`, and `systemctl --user start redprobe` to start it again.

#### Updating

The probe does not update itself. To install a new version, download the file and restart the service (settings, id and secret stay as they are):

```sh
f=$HOME/.local/share/redprobe/redprobe.py; curl -fsSL https://raw.githubusercontent.com/redredline/redprobe/v0.3.1/redprobe.py -o $f.new && echo "a5d65d6bf9325ddfc3768b43f9350f7768b93e5f93378a7c8ce5856189ebc1a9  $f.new" | sha256sum -c - && mv $f.new $f && systemctl --user restart redprobe
```

The command checks the file's checksum and replaces the probe only when it matches. If you installed with `git clone`, this works too: `git pull` in the repository folder, then `cp redprobe.py ~/.local/share/redprobe/` and `systemctl --user restart redprobe`.

#### Removing it

```sh
systemctl --user disable --now redprobe
rm -rf ~/.local/share/redprobe ~/.config/redprobe ~/.config/systemd/user/redprobe.service
systemctl --user daemon-reload
```

If no other service of yours needs lingering, turn it off with `loginctl disable-linger`. To switch the probe off on the Atlas server, use «Мои точки» in [@redatlas_robot](https://t.me/redatlas_robot).

### Settings

Settings live in `~/.config/redprobe/redprobe.conf` as `KEY=value`; environment variables with the same names work too. Restart the probe after editing the file: `systemctl --user restart redprobe`.

- `REDPROBE_SERVER` sets the server address, several can be given comma separated. The default is the relay `https://probe.redstudio.site:8444`, then `https://atlas.redstudio.site` if the relay does not answer. When one of these two is set, the probe adds the other as a fallback itself.
- `REDPROBE_NAME` sets the probe name, which only we see. It defaults to the host name.
- `REDPROBE_REGION` sets the region, for example `ru-msk`.
- `REDPROBE_ENROLL_TOKEN` holds the token until registration, then the probe removes it.
- `REDPROBE_KINDS` lists, comma separated, the job kinds the probe runs; the default is `atlas.ping,atlas.tls,atlas.tls1620`. `REDPROBE_PROXY` and the other `REDPROBE_PROXY_*` settings are for our own mobile points behind proxies; volunteers do not need them.
- `REDPROBE_MAX_PPS` caps how many checks the probe starts per second, 20 by default.
- `REDPROBE_WORKERS` caps how many checks run at the same time, 16 by default.
- `REDPROBE_MAX_JOBS` sets how many jobs the probe asks for per batch, 500 by default.

The `REDPROBE_CONF` variable changes the path to the settings file.

The server remembers the job kinds at registration. To skip the 16-20 test and its traffic from the start, install like this:

```sh
REDPROBE_KINDS=atlas.ping sh install.sh https://probe.redstudio.site:8444 TOKEN NAME REGION
```

If you remove a kind from `REDPROBE_KINDS` after registration, the server keeps sending those jobs and the probe skips them and notes it in the log.

You can also run the probe by hand: `python3 ~/.local/share/redprobe/redprobe.py --once` runs one batch and exits, without a flag it runs forever, and `--register` only registers the probe.

### How the results are used

Ping results go into a subnet's check history in Atlas, next to the RIPE Atlas and Globalping checks. Where the subnet list comes from, how a subnet gets its colour and how many vantage points it takes to call a subnet blocked in Russia is described in the [methodology](https://atlas.redstudio.site/method/) (in Russian). All Atlas data is free to take through the [open API](https://atlas.redstudio.site/api/).

### Questions

**Is it legal?** The probe does what `ping` and `curl` do: it contacts hosters' public addresses and downloads their public test files. It circumvents nothing, relays no traffic and opens no websites. Hundreds of RIPE Atlas probes run similar measurements in Russia. We are not lawyers and cannot give guarantees, so if in doubt, do not run it.

**Will anyone see my IP?** The Atlas server sees it on every request and does not store it. At registration the address goes to RIPEstat once to look up the network and country. Hosters whose addresses the probe checks see your IP as the source of the ping or connection, as with any traffic to their addresses. The Atlas site and API show neither your IP nor the probe's name.

**Can I get a scanning complaint?** You can. The probe contacts hundreds of addresses in different networks within minutes, and some hosters' intrusion detection treats that as a scan. We keep the rate low and check one address per subnet, but a complaint about your IP cannot be ruled out. If you run the probe on a rented server, read your hoster's rules. `REDPROBE_MAX_PPS` and `REDPROBE_MAX_JOBS` lower the rate.

**How much traffic?** About 40 to 80 MB a day. When the 16-20 test is switched on, add about 270 MB a day unless you turned it off.

**What if the Atlas server gets hacked?** The probe runs only its own job kinds: a ping to one address, a TCP connect to ports 443, 80 and 22, and a download of up to 256 KB over TLS. A hijacked server could point those checks at other addresses, but it could not run commands on your computer or open a port through the probe. The pace of checks stays capped by `REDPROBE_MAX_PPS` either way.

### Protocol

JSON over HTTPS:

- `POST /probe/v1/register` with `{"enroll_token", "name", "region", "version", "kinds"}` returns `{"probe_id", "secret"}`;
- `GET /probe/v1/jobs?max=N` returns `{"jobs": [...], "next_in": seconds}`;
- `POST /probe/v1/results` with `{"results": [...]}` returns `{"stored": n}`.

After registration requests are signed with the headers `X-Probe-Id`, `X-Probe-Ts` and `X-Probe-Sig`, where the signature is hex HMAC-SHA256(secret, `"<id>|<ts>|<body>"`). The server rejects signatures older than two minutes.

### License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 RedLine Studio.
