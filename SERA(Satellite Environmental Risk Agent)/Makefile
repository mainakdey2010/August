PROJECT_ID ?= august-505217
REGION ?= asia-south1
AR_REPOSITORY ?= sera
SERVICE ?= sera

IMAGE_BASE := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPOSITORY)/$(SERVICE)

.PHONY: help check build builds deploy url test all

help:
	@echo ""
	@echo "SERA commands"
	@echo "-------------"
	@echo "make check   - Validate build prerequisites"
	@echo "make build   - Build and push SERA image with Cloud Build"
	@echo "make builds  - Show recent Cloud Build executions"
	@echo "make deploy  - Deploy latest image to Cloud Run"
	@echo "make url     - Show Cloud Run URL"
	@echo "make test    - Curl deployed service"
	@echo "make all     - Build, deploy and test"
	@echo ""

check:
	@echo "Project:  $(PROJECT_ID)"
	@echo "Region:   $(REGION)"
	@echo "Image:    $(IMAGE_BASE):latest"
	@test -f cloudbuild.yaml || (echo "ERROR: cloudbuild.yaml missing" && exit 1)
	@test -f Dockerfile || (echo "ERROR: Dockerfile missing" && exit 1)
	@echo "Build prerequisites OK."

build: check
	gcloud builds submit . \
		--config=cloudbuild.yaml \
		--region=$(REGION) \
		--project=$(PROJECT_ID)

builds:
	gcloud builds list \
		--region=$(REGION) \
		--project=$(PROJECT_ID) \
		--limit=10

deploy:
	gcloud run deploy $(SERVICE) \
		--image=$(IMAGE_BASE):latest \
		--region=$(REGION) \
		--project=$(PROJECT_ID) \
		--platform=managed

url:
	gcloud run services describe $(SERVICE) \
		--region=$(REGION) \
		--project=$(PROJECT_ID) \
		--format='value(status.url)'

test:
	@URL=$$(gcloud run services describe $(SERVICE) \
		--region=$(REGION) \
		--project=$(PROJECT_ID) \
		--format='value(status.url)'); \
	echo "Testing $$URL"; \
	curl -i "$$URL"

all: build deploy test
