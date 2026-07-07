# Container for relaunching the pgu director/implementer team in isolation,
# instead of running --dangerously-skip-permissions directly on the bare host.
# Arch-based to match cmake_minimum_required(VERSION 4.3) in CMakeLists.txt --
# Arch's rolling repos track current cmake; Ubuntu/Debian stable lag behind it.
FROM archlinux:latest

RUN pacman -Syu --noconfirm && \
    pacman -S --noconfirm --needed \
        base-devel cmake ninja clang git tmux fish python openssh \
        libx11 libxrandr libxinerama libxcursor libxi libxkbcommon \
        wayland wayland-protocols \
        vulkan-icd-loader vulkan-headers mesa \
        kwin xorg-xwayland xdotool wmctrl imagemagick \
    && pacman -Scc --noconfirm

ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -g ${USER_GID} eric && \
    useradd -m -u ${USER_UID} -g ${USER_GID} -s /usr/bin/fish eric

# Known-working Claude Code binary, copied from the host rather than fetched
# fresh, since we already have a verified-good copy.
COPY claude-bin/claude /opt/claude-code/bin/claude
RUN chmod +x /opt/claude-code/bin/claude && \
    ln -s /opt/claude-code/bin/claude /usr/local/bin/claude

COPY entrypoint.sh /usr/local/bin/team-entrypoint.sh
RUN chmod +x /usr/local/bin/team-entrypoint.sh

RUN mkdir -p /home/eric/Projects && chown eric:eric /home/eric/Projects

USER eric
WORKDIR /home/eric/Projects
ENTRYPOINT ["/usr/local/bin/team-entrypoint.sh"]
