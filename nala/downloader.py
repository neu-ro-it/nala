#                 __
#    ____ _____  |  | _____
#   /    \\__  \ |  | \__  \
#  |   |  \/ __ \|  |__/ __ \_
#  |___|  (____  /____(____  /
#       \/     \/          \/
#
# Copyright (C) 2021, 2022 Blake Lee
#
# This file is part of nala
#
# nala is program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# nala is program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with nala.  If not, see <https://www.gnu.org/licenses/>.
"""Module for package downloader."""
from __future__ import annotations

import asyncio
import re
import sys
from asyncio import AbstractEventLoop, CancelledError, Semaphore, gather, run
from errno import ENOENT
from functools import partial
from pathlib import Path
from random import shuffle
from signal import Signals  # pylint: disable=no-name-in-module #Codacy
from signal import SIGINT, SIGTERM
from typing import Pattern, Union, cast

import apt_pkg
from anyio import open_file
from apt.package import Package, Version
from httpx import (URL, AsyncClient, ConnectError, ConnectTimeout, HTTPError,
				HTTPStatusError, Proxy, RemoteProtocolError, RequestError, get)
from rich.panel import Panel

from nala.constants import (ARCHIVE_DIR, ERRNO_PATTERN,
				ERROR_PREFIX, PARTIAL_DIR, FileDownloadError, _)
from nala.error import ExitCode
from nala.options import arguments
from nala.rich import Live, Table, from_ansi, pkg_download_progress
from nala.utils import (check_pkg, color, dprint, eprint,
				get_pkg_name, pkg_candidate, term, unit_str, vprint)

MIRROR_PATTERN = re.compile(r'mirror://([A-Za-z_0-9.-]+).*')

TOTAL_PACKAGES = color(_('Total Packages:'), 'GREEN')
STARTING_DOWNLOADS = color(_('Starting Downloads...'), 'BLUE')
STARTING_DOWNLOAD = color(_('Starting Download:'), 'BLUE')
LAST_COMPLETED = color(_('Last Completed:'), 'GREEN')
MIRROR_TIMEOUT = color(_('Mirror Timedout:'), 'YELLOW')
DOWNLOAD_COMPLETE = color(_('Download Complete:'), 'GREEN')
TRYING = color(_('Trying:'))
NO_MORE_MIRRORS = color(_('No More Mirrors:'), 'RED')

FILE_NO_EXIST = _("{error} {filename} Does not exist!")
HASH_MISMATCH = _("{error} File Hash Sum does not match: {filename}")
SIZE_WRONG = _("{error} File has unexpected size: {filename}")
REMOVING_FILE = _("  We have removed {filename} but will try another mirror")
FAILED_MOVE = _("{error} Failed to move archive file, {str_err}: '{file1}' -> '{file2}'")

DownloadErrorTypes = Union[
	HTTPError, HTTPStatusError, RequestError, OSError, ConnectError, FileDownloadError
]

class PkgDownloader: # pylint: disable=too-many-instance-attributes
	"""Manage Package Downloads."""

	def __init__(self, pkgs: list[Package]) -> None:
		"""Manage Package Downloads."""
		self.pkgs = pkgs
		self.total_pkgs: int = len(self.pkgs)
		self.count: int = 0
		self.live: Live
		self.mirrors: list[str] = []
		self.last_completed: str = ''
		self.task = pkg_download_progress.add_task(
			"", total=sum(pkg_candidate(pkg).size for pkg in self.pkgs)
		)
		self.pkg_urls: list[list[Version | str]] = []
		self._set_pkg_urls()
		self.pkg_urls = sorted(self.pkg_urls, key=sort_pkg_size, reverse=True)
		self.proxy: dict[URL | str, URL | str | Proxy | None] = {}
		self.failed: list[str] = []
		self.exit: int | bool = False
		self._set_proxy()

	async def start_download(self) -> bool:
		"""Start async downloads."""
		if not self.pkgs:
			return True
		semaphore = Semaphore(min(guess_concurrent(self.pkg_urls), 16))
		with Live(get_renderable=self._gen_table) as self.live:
			async with AsyncClient(timeout=20, proxies=self.proxy) as client:
				loop = asyncio.get_running_loop()
				tasks = (
					loop.create_task(
						self._init_download(client, urls, semaphore)
					) for urls in self.pkg_urls
				)
				# Setup handlers for Interrupts
				for signal_enum in (SIGINT, SIGTERM):
					exit_func = partial(self.interrupt, signal_enum, loop)
					loop.add_signal_handler(signal_enum, exit_func)

				return all(await gather(*tasks))

	async def _stream_deb(self, client: AsyncClient, url: str, dest: Path) -> int:
		"""Stream the deb package and write it to file."""
		total_data = 0
		async with client.stream('GET', url) as response:
			async with await open_file(dest, mode="wb") as file:
				async for data in response.aiter_bytes():
					if data:
						await file.write(data)
						len_data = len(data)
						total_data += len_data
						await self._update_progress(len_data)
		return total_data

	async def _download(self,
		client: AsyncClient, semaphore: Semaphore,
		candidate: Version, url: str) -> int:
		"""Download and write package."""
		dest = PARTIAL_DIR / get_pkg_name(candidate)
		async with semaphore:
			vprint(f"{STARTING_DOWNLOAD} {url} {unit_str(candidate.size, 1)}")
			assert isinstance(url, str)
			second_attempt = False
			while True:
				try:
					total_data = await self._stream_deb(client, url, dest)
					break
				# Sometimes mirrors play a little dirty and close the connection
				# Before we're done, so we catch this and try one more time.
				except RemoteProtocolError as error:
					if 'Server disconnected' not in str(error) or second_attempt:
						raise error from error
					second_attempt = True
					dprint(f"Mirror Failed: {url[:url.index('/pool')]} {error}, will try again.")
					continue
		return total_data

	async def _init_download(self, client: AsyncClient,
		urls: list[Version | str], semaphore: Semaphore) -> None:
		"""Download pkgs."""
		candidate = urls.pop(0)
		assert isinstance(candidate, Version)
		for num, url in enumerate(urls):
			assert isinstance(url, str)
			total_data = 0
			try:
				total_data = await self._download(client, semaphore, candidate, url)

				if not await process_downloads(candidate):
					await self._update_progress(total_data, failed=True)
					continue
				if not check_pkg(ARCHIVE_DIR, candidate, download=True):
					continue

				vprint(f"{DOWNLOAD_COMPLETE} {url}")

				self.count += 1
				self.last_completed = Path(candidate.filename).name
				self.live.update(self._gen_table())
				break

			except (HTTPError, OSError, FileDownloadError) as error:
				await self._update_progress(total_data, failed=True)
				self.download_error(error, num, urls, candidate)
				continue

	def interrupt(self, signal_enum: Signals, loop: AbstractEventLoop) -> None:
		"""Shutdown the loop."""
		self.exit = 128+signal_enum.real
		if self.exit == ExitCode.SIGINT:
			term.write(term.CURSER_UP+term.CLEAR_LINE)

		self.live.stop()
		for task in asyncio.all_tasks(loop):
			task.cancel()
		eprint(
			_("Exiting due to {signal}").format(
				signal=signal_enum.name
			)
		)

	def _set_proxy(self) -> None:
		"""Set proxy configuration."""
		if http_proxy := apt_pkg.config.find('Acquire::http::Proxy'):
			self.proxy['http://'] = http_proxy
		if https_proxy := apt_pkg.config.find('Acquire::https::Proxy', http_proxy):
			self.proxy['https://'] = https_proxy
		if ftp_proxy := apt_pkg.config.find('Acquire::ftp::Proxy'):
			self.proxy['ftp://'] = ftp_proxy

	def _set_pkg_urls(self) -> None:
		"""Set pkg_urls list."""
		for pkg in self.pkgs:
			candidate = pkg_candidate(pkg)
			urls: list[Version | str] = []
			urls.extend(self.filter_uris(candidate, MIRROR_PATTERN))
			# Randomize the urls to minimize load on a single mirror.
			shuffle(urls)
			urls.insert(0, candidate)
			self.pkg_urls.append(urls)

	def filter_uris(self, candidate: Version, pattern: Pattern[str]) -> list[str]:
		"""Filter uris into usable urls."""
		urls: list[str] = []
		for uri in candidate.uris:
			# Regex to check if we're using mirror.txt
			if regex := pattern.search(uri):
				domain = regex.group(1)
				if not self.mirrors:
					url = f"http://{domain}/mirrors.txt"
					try:
						self.mirrors = get(url).text.splitlines()
					except HTTPError:
						sys.exit(
							_("{error} unable to connect to {url}").format(
								error=ERROR_PREFIX, url=url
							)
						)
				urls.extend([link+candidate.filename for link in self.mirrors])
				continue
			urls.append(uri)
		return urls

	def _gen_table(self) -> Panel:
		"""Generate Rich Table."""
		table = Table.grid()

		table.add_row(
			from_ansi(f"{TOTAL_PACKAGES} {self.count}/{self.total_pkgs}")
		)
		if not self.last_completed:
			table.add_row(
					from_ansi(STARTING_DOWNLOADS)
			)
		else:
			table.add_row(from_ansi(f"{LAST_COMPLETED} {self.last_completed}"))

		pkg_download_progress.advance(self.task, advance=0)
		table.add_row(pkg_download_progress.get_renderable())
		return Panel(
			table, title='[bold white]'+_('Downloading...'), title_align='left', border_style='bold green'
		)

	def download_error(self,
		error: DownloadErrorTypes, num: int, urls: list[Version | str], candidate: Version) -> None:
		"""Handle download errors."""
		full_url = str(urls[num])
		mirror = full_url[:full_url.index('/pool')]
		print_error(error, mirror)

		if not (next_url := more_urls(urls, num, self.failed, candidate)):
			return

		vprint(f"{TRYING} {next_url}")

	async def _update_progress(self, len_data: int, failed: bool = False) -> None:
		"""Update download progress."""
		if failed:
			len_data = -len_data

		pkg_download_progress.advance(self.task, advance=len_data)
		self.live.update(self._gen_table())

def print_error(
	error: DownloadErrorTypes, mirror: str) -> None:
	"""Print the download error to console."""
	if isinstance(error, ConnectTimeout):
		vprint(f"{MIRROR_TIMEOUT} {mirror}")
		return
	if isinstance(error, ConnectError):
		# ConnectError: [Errno -2] Name or service not known
		errno_replace = re.sub(ERRNO_PATTERN, '', str(error)).strip()+':'
		vprint(f"{color(errno_replace, 'RED')} {mirror}")
		return
	if isinstance(error, FileDownloadError):
		file_error(error)
		return
	msg = str(error) or type(error).__name__
	vprint(f"{ERROR_PREFIX} {msg}")

def file_error(error: FileDownloadError) -> None:
	"""Print the error from our FileDownloadError exception."""
	if error.errno == FileDownloadError.ENOENT:
		eprint(
			FILE_NO_EXIST.format(error=ERROR_PREFIX, filename=error.filename)
		)
		return
	if error.errno == FileDownloadError.ERRHASH:
		eprint(
			HASH_MISMATCH.format(error=ERROR_PREFIX, filename=error.filename)
		)

	if error.errno == FileDownloadError.ERRSIZE:
		eprint(
			SIZE_WRONG.format(error=ERROR_PREFIX, filename=error.filename)
		)

	eprint(
		REMOVING_FILE.format(filename=error.filename)
	)

def more_urls(urls: list[Version | str], num: int,
	failed: list[str], candidate: Version) -> str | bool:
	"""Check if there is another url to try. Return False if not."""
	try:
		return cast(str, urls[num+1])
	except IndexError:
		filename = Path(candidate.filename).name
		eprint(
			_("{error} No more mirrors available for {filename}").format(
				error=ERROR_PREFIX, filename=filename
			)
		)
		eprint(_("  Apt will try to fetch the package before installation"))
		failed.append(filename)
		return False

async def process_downloads(candidate: Version) -> bool:
	"""Process the downloaded packages."""
	filename = get_pkg_name(candidate)
	destination = ARCHIVE_DIR / filename
	source = PARTIAL_DIR / filename
	try:
		dprint(f'Moving {source} -> {destination}')
		source.rename(destination)
	except OSError as error:
		if error.errno != ENOENT:
			eprint(
				FAILED_MOVE.format(
					error=ERRNO_PATTERN, str_err=error.strerror,
					file1=error.filename, file2=error.filename2
				)
			)
		return False
	return True

def guess_concurrent(pkg_urls: list[list[Version | str]]) -> int:
	"""Determine how many concurrent downloads to do."""
	max_uris = 2
	for pkg in pkg_urls:
		max_uris = max(len(pkg[1:])*2, max_uris)
	return max_uris

def sort_pkg_size(pkg_url: list[Version | str]) -> int:
	"""Sort by package size.

	This is to be used as sorted(key=sort_pkg_size)
	"""
	candidate = pkg_url[0]
	assert isinstance(candidate, Version)
	assert isinstance(candidate.size, int)
	return candidate.size

def download(pkgs: list[Package]) -> None:
	"""Run downloads and check for failures.

	Does not return if in Download Only mode.
	"""
	downloader = PkgDownloader(pkgs)
	try:
		run(downloader.start_download())
	except CancelledError as error:
		if downloader.exit:
			sys.exit(downloader.exit)
		raise error from error

	if arguments.download_only:
		if downloader.failed:
			for pkg in downloader.failed:
				eprint(
					_("{error} {pkg} Failed to download").format(
						error=ERROR_PREFIX, pkg=pkg
					)
				)
			sys.exit(
				_("{error} Some downloads failed and in download only mode.").format(
					error=ERROR_PREFIX
				)
			)
		print(_("Download complete and in download only mode."))
		sys.exit(0)

	if downloader.failed:
		eprint(
			_("{warning} Falling back to apt_pkg. The following downloads failed:").format(
				warning = color(_('Warning:'), 'YELLOW')
			)
		)
		eprint(
			f"  {', '.join(color(pkg, 'YELLOW') for pkg in downloader.failed)}"
			)
