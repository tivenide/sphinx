FROM node:22.23.1-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f
# High ports need no file capability; retaining it prevents exec with cap_drop: ALL.
RUN setcap -r /usr/bin/caddy \
    && addgroup -S -g 10001 sphinx && adduser -S -D -H -u 10001 -G sphinx sphinx \
    && mkdir -p /data /config /srv && chown -R sphinx:sphinx /data /config /srv
COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
USER 10001:10001
EXPOSE 8080 8443
