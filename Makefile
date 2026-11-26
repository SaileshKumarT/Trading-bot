.PHONY: build up down logs shell migrate dashboard

# Build Docker images
build:
	docker-compose build

# Start all services in detached mode
up:
	docker-compose up -d

# Stop all services
down:
	docker-compose down

# Tail bot logs (live follow)
logs:
	docker-compose logs -f bot

# Open a shell inside the bot container
shell:
	docker exec -it delta_trading_bot /bin/bash

# Run Alembic database migrations
migrate:
	docker exec -it delta_trading_bot alembic upgrade head

# Open the live dashboard in the default browser
dashboard:
	@echo "Opening dashboard at http://localhost:8080 ..."
	@command -v xdg-open >/dev/null 2>&1 && xdg-open http://localhost:8080 || \
	 command -v open >/dev/null 2>&1 && open http://localhost:8080 || \
	 start http://localhost:8080 2>/dev/null || \
	 echo "Visit http://localhost:8080 in your browser"
