FROM node:22-trixie-slim AS build

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

RUN apk add --no-cache openssh-server \
    && mkdir -p /run/sshd /root/.ssh \
    && chmod 700 /root/.ssh \
    && printf '%s\n' \
      'Port 22' \
      'ListenAddress 0.0.0.0' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' \
      'ChallengeResponseAuthentication no' \
      'PermitRootLogin prohibit-password' \
      'PubkeyAuthentication yes' \
      'AuthorizedKeysFile /root/.ssh/authorized_keys' \
      'AllowUsers root' \
      'X11Forwarding no' \
      'AllowTcpForwarding no' \
      'PrintMotd no' \
      'UsePAM no' \
      'PidFile /run/sshd.pid' \
      > /etc/ssh/sshd_config

COPY --from=build /app/out /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY start_web.sh /app/start_web.sh
RUN chmod +x /app/start_web.sh

EXPOSE 80
EXPOSE 22

CMD ["/app/start_web.sh"]
