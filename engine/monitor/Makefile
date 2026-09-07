VENDOR_DIR = ../vendor

CC ?= gcc
CFLAGS ?= -Wall -Wextra -std=c11 -O2
CPPFLAGS += -I$(VENDOR_DIR)

TARGET = clipper-monitor-host
SRCS = clipper_monitor_host.c $(VENDOR_DIR)/cJSON.c

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $(SRCS) $(LDFLAGS)

clean:
	rm -f $(TARGET)
