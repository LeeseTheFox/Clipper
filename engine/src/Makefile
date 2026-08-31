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

# obs-ffmpeg-mux must live next to the engine binary so the replay buffer
# plugin can spawn it via os_get_executable_path_ptr() / /proc/self/exe.
MUX_LINK = obs-ffmpeg-mux
MUX_SRC  = $(OBS_PREFIX)/bin/obs-ffmpeg-mux

.PHONY: all clean install

all: $(TARGET) $(MUX_LINK)

$(TARGET): $(SRCS)
	$(CC) $(CFLAGS) -o $@ $(SRCS) $(LDFLAGS)

$(MUX_LINK):
	ln -sf $(MUX_SRC) $(MUX_LINK)

install: all
	install -Dm755 $(TARGET) $(DESTDIR)$(LIBEXECDIR)/$(TARGET)
	mkdir -p $(DESTDIR)$(LIBEXECDIR)
	ln -sf $(OBS_PREFIX)/bin/obs-ffmpeg-mux $(DESTDIR)$(LIBEXECDIR)/obs-ffmpeg-mux

clean:
	rm -f $(TARGET) $(MUX_LINK)
