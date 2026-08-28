FROM node:22-bookworm-slim

WORKDIR /repo/apps/frontend

COPY apps/frontend/package.json apps/frontend/package-lock.json* /repo/apps/frontend/
RUN npm install

COPY apps/frontend /repo/apps/frontend

EXPOSE 5173
CMD ["npm", "run", "dev"]
