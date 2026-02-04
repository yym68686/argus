FROM node:22-bookworm-slim AS build

WORKDIR /app

ARG NEXT_PUBLIC_ARGUS_WS_URL
ENV NEXT_PUBLIC_ARGUS_WS_URL=$NEXT_PUBLIC_ARGUS_WS_URL

COPY package.json /app/package.json
RUN npm install

COPY tsconfig.json /app/tsconfig.json
COPY next.config.ts /app/next.config.ts
COPY postcss.config.mjs /app/postcss.config.mjs
COPY tailwind.config.ts /app/tailwind.config.ts
COPY eslint.config.mjs /app/eslint.config.mjs
COPY next-env.d.ts /app/next-env.d.ts
COPY src /app/src

RUN npm run build


FROM nginx:1.27-alpine

COPY --from=build /app/out /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
