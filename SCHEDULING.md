# Come schedulare `weekly_report.py` ogni settimana

## 1. Carica il file nel repo

Aggiungi `weekly_report.py` alla root del repo `trading-agent`, accanto ad
`agent.py` e `daily_report.py`.

Via GitHub web:
1. Vai su https://github.com/matteocariola8-lgtm/trading-agent
2. **Add file → Upload files** → trascina `weekly_report.py`
3. Commit message: "Aggiunge report settimanale via email"
4. **Commit directly to the main branch** → **Commit changes**

## 2. Scelta del meccanismo di scheduling

`agent.py` gira in un loop continuo (`while True` + `sleep(LOOP_INTERVAL)`),
quindi la via più semplice e pulita è aggiungere **un secondo servizio
Railway separato con cron schedule nativo**, invece di infilare la logica
settimanale dentro il loop di `agent.py` (che gira ogni 5 minuti — sarebbe
scomodo far scattare l'invio solo il lunedì da lì dentro senza rischiare
doppi invii).

### Opzione consigliata: Cron Job su Railway

1. Vai su https://railway.app/dashboard e apri il progetto **trading-agent**
2. Clicca **+ New** (in alto a destra) → **Empty Service**
3. Dai un nome al servizio, es. `weekly-report`
4. Vai su **Settings** del nuovo servizio → sezione **Source**
   → **Connect Repo** → seleziona `matteocariola8-lgtm/trading-agent`
   (stesso repo, root directory)
5. Sempre in **Settings**, sezione **Deploy**:
   - **Custom Start Command**: `python weekly_report.py`
6. Sezione **Cron Schedule** (Railway supporta cron nativo per servizio):
   - Attiva **Cron Schedule**
   - Espressione: `0 7 * * 1` (ogni lunedì alle 07:00 UTC = 08:00/09:00 ora italiana a seconda di ora legale)
   - Usa https://crontab.guru per verificare/modificare l'orario
7. Vai su **Variables** del nuovo servizio e copia le stesse variabili
   già usate da `agent.py`:
   - `EMAIL_SENDER`
   - `EMAIL_PASSWORD`
   - `MEMORY_FILE` (se vuoi puntare a un path condiviso — vedi nota sotto)

## 3. Nota importante: `memory.json` deve essere condiviso tra i due servizi

Se `agent.py` scrive `memory.json` su un **volume Railway montato**, il
nuovo servizio `weekly-report` deve montare **lo stesso volume** per poterlo
leggere, altrimenti troverà un file vuoto/inesistente.

Controlla:
1. Nel servizio principale (`agent.py`) → **Settings → Volumes** →
   controlla se c'è un volume montato e a quale path (es. `/data`)
2. Se sì: nel nuovo servizio `weekly-report`, aggiungi lo stesso volume
   allo stesso path, e imposta la variabile `MEMORY_FILE=/data/memory.json`
   su **entrambi** i servizi
3. Se `agent.py` NON usa un volume (cioè `memory.json` vive solo nel
   filesystem effimero del container e si perde ad ogni redeploy),
   allora il weekly report leggerebbe sempre un file vuoto appena dopo
   un redeploy di `agent.py` — in tal caso vale la pena aggiungere un
   volume persistente prima di procedere. Dimmelo se non sei sicuro e
   controlliamo insieme.

## 4. Test manuale prima di fidarsi dello schedule

Prima di aspettare lunedì, testa a mano:
1. Sul servizio `weekly-report` → tab **Deployments**
2. Clicca sui tre puntini del deployment attivo → verifica se c'è
   un'opzione **Trigger manually** (dipende dalla versione UI di Railway;
   se non c'è, puoi temporaneamente disattivare il cron, fare un redeploy
   che lo fa partire subito con lo start command, poi riattivare il cron)
3. Controlla la tua email per il report

## Cosa NON fa questo script

Non tocca `daily_report.py` né la sua eventuale schedulazione — è un file
separato con la sua identità nel subject email ("Report settimanale" vs
qualsiasi cosa usi `daily_report.py`), così puoi tenerli entrambi attivi
senza confusione.
