FROM node:22-trixie-slim

WORKDIR /app/apps/telegram-bot

COPY apps/telegram-bot/package.json /app/apps/telegram-bot/package.json
RUN npm install --omit=dev

COPY VERSION /app/VERSION
COPY apps/telegram-bot/index.mjs /app/apps/telegram-bot/index.mjs

ENV NODE_ENV=production

CMD ["node", "index.mjs"]
