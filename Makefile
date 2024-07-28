PYTHON_FILES=walk_pages.py compare_pages.py

default:
	@echo "Usage: make lint"

lint:
	for file in $(PYTHON_FILES); do \
		echo "$$file:"; \
		pylint $$file; \
	done
