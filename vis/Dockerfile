FROM nginx:alpine

# Copy the static dashboard
COPY index.html /usr/share/nginx/html/index.html

# Copy nginx config template (with ${API_URL} placeholder for envsubst)
COPY default.conf.template /etc/nginx/templates/default.conf.template

# Remove the default config (templates/ dir will generate it)
RUN rm -f /etc/nginx/conf.d/default.conf

EXPOSE 3000

# nginx:alpine image natively supports /etc/nginx/templates/*.template
# It runs envsubst on them at startup, outputting to /etc/nginx/conf.d/
# We just need to tell it which env vars to substitute
ENV NGINX_ENVSUBST_TEMPLATE_SUFFIX=.template
ENV API_URL=http://gateway:8000
