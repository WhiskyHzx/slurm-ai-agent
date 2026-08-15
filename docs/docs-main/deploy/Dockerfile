FROM nginx:alpine-slim
COPY deploy/nginx-docs.conf /etc/nginx/conf.d/default.conf
COPY site/ /usr/share/nginx/html/
EXPOSE 80
