OBS_PREFIX = /var/lib/flatpak/app/com.obsproject.Studio/x86_64/stable/active/files
OBS_INCDIR = $(OBS_PREFIX)/include
OBS_LIBDIR = $(OBS_PREFIX)/lib
VENDOR_DIR = ../vendor
PREFIX ?= /app
LIBEXECDIR = $(PREFIX)/libexec/clipper

CC     = gcc
GLIB_CFLAGS := $(shell pkg-config --cflags glib-2.0 gio-2.0)
GLIB_LIBS   := $(shell pkg-config --libs glib-2.0 gio-2.0)
WAYLAND_CFLAGS := $(shell pkg-config --cflags wayland-client)
WAYLAND_LIBS   := $(shell pkg-config --libs wayland-client)
CFLAGS = -Wall -Wextra -std=c11 -I$(OBS_INCDIR) -I$(VENDOR_DIR) $(GLIB_CFLAGS) $(WAYLAND_CFLAGS)
# --allow-shlib-undefined: suppresses link-time errors for transitive deps of
# libavcodec (libvpx.so.11, libtheoraenc.so.2, etc.) that are not on the host
# at the required SONAME.  They are resolved at runtime via lib_shim/.
LDFLAGS = -L$(OBS_LIBDIR) -Wl,-rpath,$(OBS_LIBDIR) \
          -Wl,--allow-shlib-undefined \
          -lobs -lpthread $(GLIB_LIBS) $(WAYLAND_LIBS)

TARGET  = clipper-engine
SRCS    = clipper_engine.c $(VENDOR_DIR)/cJSON.c

# OBS resolves subprocesses beside the running executable through
# os_get_executable_path_ptr() / /proc/self/exe. Both replay muxing and
# NVIDIA encoder detection need their helpers beside clipper-engine.
OBS_HELPERS = obs-ffmpeg-mux obs-nvenc-test

.PHONY: all clean install

all: $(TARGET) $(OBS_HELPERS)

$(TARGET): $(SRCS)
	$(CC) $(CFLAGS) -o $@ $(SRCS) $(LDFLAGS)

$(OBS_HELPERS):
	ln -sf $(OBS_PREFIX)/bin/$@ $@

install: all
	install -Dm755 $(TARGET) $(DESTDIR)$(LIBEXECDIR)/$(TARGET)
	mkdir -p $(DESTDIR)$(LIBEXECDIR)
	for helper in $(OBS_HELPERS); do \
		ln -sf $(OBS_PREFIX)/bin/$$helper $(DESTDIR)$(LIBEXECDIR)/$$helper; \
	done

clean:
	rm -f $(TARGET) $(OBS_HELPERS)
