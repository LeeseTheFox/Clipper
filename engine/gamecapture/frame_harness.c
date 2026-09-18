/* Minimal OpenGL/Vulkan frame producer for managed-payload integration tests. */

#define _DEFAULT_SOURCE
#define VK_USE_PLATFORM_XLIB_KHR

#include <GL/gl.h>
#include <GL/glx.h>
#include <X11/Xlib.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <unistd.h>
#include <vulkan/vulkan.h>

#ifndef WIDTH
#define WIDTH 320
#endif
#ifndef HEIGHT
#define HEIGHT 240
#endif
static unsigned source_fps = 60;
static unsigned frame_count = 300;
static uint64_t start_ns;

static uint64_t monotonic_ns(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000000000ULL + now.tv_nsec;
}

static void wait_frame(unsigned frame)
{
    if (!frame)
        start_ns = monotonic_ns();
    uint64_t deadline = start_ns + (uint64_t)(frame + 1) * 1000000000ULL / source_fps;
    struct timespec when = {.tv_sec = deadline / 1000000000ULL,
                           .tv_nsec = deadline % 1000000000ULL};
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &when, NULL) == EINTR) {}
}

/* Four-bit channels survive lossy video encoding; 12-bit ID wraps at 4096. */
static float frame_color(unsigned frame, unsigned shift)
{
    return (float)(((frame >> shift) & 15) * 16 + 8) / 255.0f;
}

static Display *create_window(Window *window)
{
    Display *display = XOpenDisplay(NULL);
    if (!display)
        return NULL;
    *window = XCreateSimpleWindow(display, DefaultRootWindow(display), 0, 0,
                                  WIDTH, HEIGHT, 0, 0, 0);
    /* Keep native-size tests independent of WM decorations/work-area limits. */
    XSetWindowAttributes attributes = {.override_redirect = True};
    XChangeWindowAttributes(display, *window, CWOverrideRedirect, &attributes);
    XStoreName(display, *window, "Clipper payload frame harness");
    XMapWindow(display, *window);
    XFlush(display);
    return display;
}

static int run_opengl(void)
{
    Display *display = XOpenDisplay(NULL);
    if (!display) {
        fputs("could not open X display\n", stderr);
        return 1;
    }
    int attributes[] = {GLX_RGBA, GLX_DOUBLEBUFFER, GLX_DEPTH_SIZE, 24, None};
    XVisualInfo *visual = glXChooseVisual(display, DefaultScreen(display), attributes);
    if (!visual) {
        fputs("could not choose GLX visual\n", stderr);
        return 1;
    }
    Colormap colormap = XCreateColormap(display, RootWindow(display, visual->screen),
                                        visual->visual, AllocNone);
    XSetWindowAttributes window_attributes = {.colormap = colormap,
                                               .override_redirect = True,
                                               .event_mask = ExposureMask};
    Window window = XCreateWindow(display, RootWindow(display, visual->screen),
                                  0, 0, WIDTH, HEIGHT, 0, visual->depth,
                                  InputOutput, visual->visual,
                                  CWColormap | CWEventMask | CWOverrideRedirect, &window_attributes);
    XStoreName(display, window, "Clipper OpenGL payload harness");
    XMapWindow(display, window);
    GLXContext context = glXCreateContext(display, visual, NULL, True);
    if (!context || !glXMakeCurrent(display, window, context)) {
        fputs("could not create GLX context\n", stderr);
        return 1;
    }
    typedef void (*swap_interval_ext)(Display *, GLXDrawable, int);
    swap_interval_ext set_interval = (swap_interval_ext)glXGetProcAddressARB(
        (const GLubyte *)"glXSwapIntervalEXT");
    if (set_interval)
        set_interval(display, window, 0);
    for (unsigned frame = 0; frame < frame_count; frame++) {
        glClearColor(frame_color(frame, 0), frame_color(frame, 4), frame_color(frame, 8), 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        /* Spatial/color sentinels outside the center frame-ID area. GL's
         * origin is bottom-left; the encoded image must have red/green at
         * the top and blue/white at the bottom, with no plane swap. */
        static const float corners[4][3] = {
            {1, 0, 0}, {0, 1, 0}, {0, 0, 1}, {1, 1, 1}
        };
        glEnable(GL_SCISSOR_TEST);
        for (unsigned i = 0; i < 4; ++i) {
            glScissor((i & 1) * WIDTH / 2, i < 2 ? HEIGHT * 3 / 4 : 0,
                      WIDTH / 2, HEIGHT / 4);
            glClearColor(corners[i][0], corners[i][1], corners[i][2], 1);
            glClear(GL_COLOR_BUFFER_BIT);
        }
        glDisable(GL_SCISSOR_TEST);
        glXSwapBuffers(display, window);
        wait_frame(frame);
    }
    glXMakeCurrent(display, None, NULL);
    glXDestroyContext(display, context);
    XDestroyWindow(display, window);
    XCloseDisplay(display);
    return 0;
}

static bool vk_ok(VkResult result, const char *operation)
{
    if (result == VK_SUCCESS || result == VK_SUBOPTIMAL_KHR)
        return true;
    fprintf(stderr, "%s failed with Vulkan result %d\n", operation, result);
    return false;
}

static int run_vulkan(void)
{
    Window window;
    Display *display = create_window(&window);
    if (!display) {
        fputs("could not open X display\n", stderr);
        return 1;
    }

    const char *instance_extensions[] = {
        VK_KHR_SURFACE_EXTENSION_NAME,
        VK_KHR_XLIB_SURFACE_EXTENSION_NAME,
    };
    VkApplicationInfo application = {
        .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "Clipper payload frame harness",
        .apiVersion = VK_API_VERSION_1_1,
    };
    VkInstanceCreateInfo instance_info = {
        .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pApplicationInfo = &application,
        .enabledExtensionCount = 2,
        .ppEnabledExtensionNames = instance_extensions,
    };
    VkInstance instance;
    if (!vk_ok(vkCreateInstance(&instance_info, NULL, &instance), "vkCreateInstance"))
        return 1;

    VkXlibSurfaceCreateInfoKHR surface_info = {
        .sType = VK_STRUCTURE_TYPE_XLIB_SURFACE_CREATE_INFO_KHR,
        .dpy = display,
        .window = window,
    };
    VkSurfaceKHR surface;
    if (!vk_ok(vkCreateXlibSurfaceKHR(instance, &surface_info, NULL, &surface),
               "vkCreateXlibSurfaceKHR"))
        return 1;

    uint32_t physical_count = 0;
    if (!vk_ok(vkEnumeratePhysicalDevices(instance, &physical_count, NULL),
               "vkEnumeratePhysicalDevices") || physical_count == 0)
        return 1;
    VkPhysicalDevice physical_devices[8];
    if (physical_count > 8)
        physical_count = 8;
    if (!vk_ok(vkEnumeratePhysicalDevices(instance, &physical_count, physical_devices),
               "vkEnumeratePhysicalDevices"))
        return 1;
    VkPhysicalDevice physical = physical_devices[0];

    uint32_t queue_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &queue_count, NULL);
    VkQueueFamilyProperties queue_properties[32];
    if (queue_count > 32)
        queue_count = 32;
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &queue_count, queue_properties);
    uint32_t queue_family = UINT32_MAX;
    for (uint32_t index = 0; index < queue_count; index++) {
        VkBool32 present = VK_FALSE;
        vkGetPhysicalDeviceSurfaceSupportKHR(physical, index, surface, &present);
        if (present && (queue_properties[index].queueFlags & VK_QUEUE_GRAPHICS_BIT)) {
            queue_family = index;
            break;
        }
    }
    if (queue_family == UINT32_MAX) {
        fputs("no graphics/present queue family\n", stderr);
        return 1;
    }

    float priority = 1.0f;
    VkDeviceQueueCreateInfo queue_info = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = queue_family,
        .queueCount = 1,
        .pQueuePriorities = &priority,
    };
    const char *device_extensions[] = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
    VkDeviceCreateInfo device_info = {
        .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1,
        .pQueueCreateInfos = &queue_info,
        .enabledExtensionCount = 1,
        .ppEnabledExtensionNames = device_extensions,
    };
    VkDevice device;
    if (!vk_ok(vkCreateDevice(physical, &device_info, NULL, &device), "vkCreateDevice"))
        return 1;
    VkQueue queue;
    vkGetDeviceQueue(device, queue_family, 0, &queue);

    VkSurfaceCapabilitiesKHR capabilities;
    if (!vk_ok(vkGetPhysicalDeviceSurfaceCapabilitiesKHR(physical, surface, &capabilities),
               "vkGetPhysicalDeviceSurfaceCapabilitiesKHR"))
        return 1;
    uint32_t format_count = 0;
    vkGetPhysicalDeviceSurfaceFormatsKHR(physical, surface, &format_count, NULL);
    VkSurfaceFormatKHR formats[32];
    if (format_count > 32)
        format_count = 32;
    if (!vk_ok(vkGetPhysicalDeviceSurfaceFormatsKHR(physical, surface, &format_count, formats),
               "vkGetPhysicalDeviceSurfaceFormatsKHR") || format_count == 0)
        return 1;
    VkExtent2D extent = capabilities.currentExtent;
    for (unsigned i = 0; i < format_count; ++i) {
        if (formats[i].format == VK_FORMAT_B8G8R8A8_UNORM ||
            formats[i].format == VK_FORMAT_R8G8B8A8_UNORM) {
            formats[0] = formats[i];
            break;
        }
    }
    if (extent.width == UINT32_MAX)
        extent = (VkExtent2D){WIDTH, HEIGHT};
    uint32_t image_count = capabilities.minImageCount + 1;
    if (capabilities.maxImageCount && image_count > capabilities.maxImageCount)
        image_count = capabilities.maxImageCount;
    VkCompositeAlphaFlagBitsKHR composite = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
    if (!(capabilities.supportedCompositeAlpha & composite))
        composite = (VkCompositeAlphaFlagBitsKHR)(capabilities.supportedCompositeAlpha &
                                                  -capabilities.supportedCompositeAlpha);

    uint32_t mode_count = 0;
    vkGetPhysicalDeviceSurfacePresentModesKHR(physical, surface, &mode_count, NULL);
    VkPresentModeKHR modes[32];
    if (mode_count > 32)
        mode_count = 32;
    vkGetPhysicalDeviceSurfacePresentModesKHR(physical, surface, &mode_count, modes);
    VkPresentModeKHR present_mode = VK_PRESENT_MODE_FIFO_KHR;
    for (unsigned i = 0; i < mode_count; ++i)
        if (modes[i] == VK_PRESENT_MODE_IMMEDIATE_KHR)
            present_mode = modes[i];
    VkSwapchainCreateInfoKHR swapchain_info = {
        .sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR,
        .surface = surface,
        .minImageCount = image_count,
        .imageFormat = formats[0].format,
        .imageColorSpace = formats[0].colorSpace,
        .imageExtent = extent,
        .imageArrayLayers = 1,
        .imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
        .imageSharingMode = VK_SHARING_MODE_EXCLUSIVE,
        .preTransform = capabilities.currentTransform,
        .compositeAlpha = composite,
        .presentMode = present_mode,
        .clipped = VK_TRUE,
    };
    VkSwapchainKHR swapchain;
    if (!vk_ok(vkCreateSwapchainKHR(device, &swapchain_info, NULL, &swapchain),
               "vkCreateSwapchainKHR"))
        return 1;
    VkSemaphoreCreateInfo semaphore_info = {
        .sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO,
    };
    VkSemaphore available;
    if (!vk_ok(vkCreateSemaphore(device, &semaphore_info, NULL, &available),
               "vkCreateSemaphore"))
        return 1;

    VkSemaphore rendered;
    if (!vk_ok(vkCreateSemaphore(device, &semaphore_info, NULL, &rendered),
               "vkCreateSemaphore"))
        return 1;
    VkCommandPoolCreateInfo pool_info = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,
        .queueFamilyIndex = queue_family,
    };
    VkCommandPool pool;
    if (!vk_ok(vkCreateCommandPool(device, &pool_info, NULL, &pool), "vkCreateCommandPool"))
        return 1;
    VkCommandBufferAllocateInfo allocation = {
        .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = pool,
        .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
        .commandBufferCount = 1,
    };
    VkCommandBuffer commands;
    if (!vk_ok(vkAllocateCommandBuffers(device, &allocation, &commands), "vkAllocateCommandBuffers"))
        return 1;
    VkImage images[16];
    uint32_t images_count = 16;
    if (!vk_ok(vkGetSwapchainImagesKHR(device, swapchain, &images_count, images), "vkGetSwapchainImagesKHR"))
        return 1;

    for (unsigned frame = 0; frame < frame_count; frame++) {
        uint32_t image_index;
        VkResult acquired = vkAcquireNextImageKHR(device, swapchain, UINT64_MAX,
                                                   available, VK_NULL_HANDLE,
                                                   &image_index);
        if (!vk_ok(acquired, "vkAcquireNextImageKHR"))
            return 1;
        vkResetCommandBuffer(commands, 0);
        VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        vkBeginCommandBuffer(commands, &begin);
        VkImageSubresourceRange range = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        VkImageMemoryBarrier barrier = {
            .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
            .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED,
            .newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            .dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
            .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
            .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
            .image = images[image_index],
            .subresourceRange = range,
        };
        vkCmdPipelineBarrier(commands, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                             VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0, NULL, 1, &barrier);
        VkClearColorValue color = {.float32 = {
            frame_color(frame, 0), frame_color(frame, 4), frame_color(frame, 8), 1.0f}};
        vkCmdClearColorImage(commands, images[image_index], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                            &color, 1, &range);
        barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        barrier.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
        barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        barrier.dstAccessMask = 0;
        vkCmdPipelineBarrier(commands, VK_PIPELINE_STAGE_TRANSFER_BIT,
                             VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, NULL, 0, NULL, 1, &barrier);
        vkEndCommandBuffer(commands);
        VkPipelineStageFlags wait_stage = VK_PIPELINE_STAGE_TRANSFER_BIT;
        VkSubmitInfo submit = {
            .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
            .waitSemaphoreCount = 1,
            .pWaitSemaphores = &available,
            .pWaitDstStageMask = &wait_stage,
            .commandBufferCount = 1,
            .pCommandBuffers = &commands,
            .signalSemaphoreCount = 1,
            .pSignalSemaphores = &rendered,
        };
        if (!vk_ok(vkQueueSubmit(queue, 1, &submit, VK_NULL_HANDLE), "vkQueueSubmit"))
            return 1;
        VkPresentInfoKHR present = {
            .sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR,
            .waitSemaphoreCount = 1,
            .pWaitSemaphores = &rendered,
            .swapchainCount = 1,
            .pSwapchains = &swapchain,
            .pImageIndices = &image_index,
        };
        if (!vk_ok(vkQueuePresentKHR(queue, &present), "vkQueuePresentKHR"))
            return 1;
        vkQueueWaitIdle(queue);
        wait_frame(frame);
    }

    vkDeviceWaitIdle(device);
    vkDestroyCommandPool(device, pool, NULL);
    vkDestroySemaphore(device, rendered, NULL);
    vkDestroySemaphore(device, available, NULL);
    vkDestroySwapchainKHR(device, swapchain, NULL);
    vkDestroyDevice(device, NULL);
    vkDestroySurfaceKHR(instance, surface, NULL);
    vkDestroyInstance(instance, NULL);
    XDestroyWindow(display, window);
    XCloseDisplay(display);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2 || argc > 4 || (strcmp(argv[1], "opengl") && strcmp(argv[1], "vulkan"))) {
        fputs("usage: frame-harness opengl|vulkan [fps [seconds]]\n", stderr);
        return 64;
    }
    if (argc > 2) {
        source_fps = (unsigned)atoi(argv[2]);
        unsigned seconds = argc > 3 ? (unsigned)atoi(argv[3]) : 5;
        if (!source_fps || source_fps > 1000 || !seconds || seconds > 60)
            return 64;
        frame_count = source_fps * seconds;
    }
    return strcmp(argv[1], "opengl") == 0 ? run_opengl() : run_vulkan();
}
