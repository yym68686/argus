FROM node:22-bookworm-slim

WORKDIR /app

COPY package.json /app/package.json
RUN npm install --omit=dev

COPY index.mjs /app/index.mjs

ENV NODE_ENV=production

CMD ["node", "index.mjs"]

