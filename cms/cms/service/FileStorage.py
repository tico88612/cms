#!/usr/bin/python
# -*- coding: utf-8 -*-

# Programming contest management system
# Copyright © 2010-2011 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2011 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2011 Matteo Boscariol <boscarim@hotmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""FileStorage is service to store and retrieve files, assumed to be binary.

"""

import os
import sys

import tempfile
import shutil
import codecs
import hashlib
import cStringIO

from cms.async.AsyncLibrary import Service, \
     rpc_method, rpc_binary_response, rpc_callback, \
     logger
from cms.async import ServiceCoord, Address, Config,\
     make_async, async_response, async_error
from cms.service.Utils import mkdir, random_string


class FileStorage(Service):
    """Offer capabilities of storing and retrieving binary files.

    """

    def __init__(self, shard):
        logger.initialize(ServiceCoord("FileStorage", shard))
        logger.debug("FileStorage.__init__")
        Service.__init__(self, shard)

        # Create server directories
        self.base_dir = os.path.join(Config._data_dir, "fs")
        self.tmp_dir = os.path.join(self.base_dir, "tmp")
        self.obj_dir = os.path.join(self.base_dir, "objects")
        self.desc_dir = os.path.join(self.base_dir, "descriptions")
        logger.info("Using %s as base directory." % self.base_dir)

        if not mkdir(Config._data_dir) or \
               not mkdir(self.base_dir) or \
               not mkdir(self.tmp_dir) or \
               not mkdir(self.obj_dir) or \
               not mkdir(self.desc_dir):
            logger.error("Cannot create necessary directories.")
            self.exit()

    @rpc_method
    def put_file(self, binary_data, description=""):
        """Method to put a file in the file storage.

        description (string): a human-readable description of the
                              content of the file (not used
                              internally, just for human convenience)
        returns (string): the SHA1 digest of the file

        """
        logger.debug("FileStorage.put")
        # Avoid too long descriptions, that can bloat our logs
        if len(description) > 1024:
            log.info("Description '%s...' trimmed because too long." %
                     description[:50])
            description = description[:1024]
        logger.info("New file added: `%s'" % description)

        # FIXME - Error management
        temp_file, temp_filename = tempfile.mkstemp(dir=self.tmp_dir)
        temp_file = os.fdopen(temp_file, "wb")

        # Get the file and compute the hash
        hasher = hashlib.sha1()
        temp_file.write(binary_data)
        hasher.update(binary_data)
        temp_file.close()
        digest = hasher.hexdigest()
        shutil.move(temp_filename, os.path.join(self.obj_dir, digest))

        # Update description
        with codecs.open(os.path.join(self.desc_dir, digest), "w",
                         "utf-8") as desc_file:
            print >> desc_file, description

        logger.debug("File with digest %s and description `%s' put" %
                     (digest, description))
        return digest

    @rpc_method
    @rpc_binary_response
    def get_file(self, digest):
        """Method to get a file from the file storage.

        digest (string): the SHA1 digest of the requested file
        returns (string): the binary string containing the content of
                          the file

        """
        logger.debug("FileStorage.get")
        logger.info("Getting file %s." % digest)
        # Errors are managed by the caller
        input_file = open(os.path.join(self.obj_dir, digest), "rb")
        data = input_file.read()
        logger.debug("File with digest %s and description `%s' retrieved" %
                     (digest, self.describe(digest)))
        return data

    @rpc_method
    def delete(self, digest):
        logger.debug("FileStorage.delete")
        logger.info("Deleting file %s." % digest)
        res = True
        try:
            os.remove(os.path.join(self.desc_dir, digest))
        except IOError:
            res = False
        try:
            os.remove(os.path.join(self.obj_dir, digest))
        except IOError:
            res = False
        return res

    @rpc_method
    def is_file_present(self, digest):
        logger.debug("FileStorage.is_file_present")
        return os.path.exists(os.path.join(self.obj_dir, digest))

    @rpc_method
    def describe(self, digest):
        logger.debug("FileStorage.describe")
        try:
            with open(os.path.join(self.desc_dir, digest)) as fd:
                return fd.read().strip()
        except IOError:
            return None


class FileCacherSync:
    """This class implement a local cache for files obtainable from a
    FileStorage service. This class uses the make_async decorator,
    hence it may be called as the operations it does were
    synchronous. One has to be aware that some other code may be
    executed between the call and the return.

    """

    def __init__(self, service, file_storage):
        """Initialization.

        service (Service): the service we are running in.
        file_storage (RemoteService): the local instance of the
                                      FileStorage service.

        """
        self.service = service
        self.file_storage = file_storage
        self.base_dir = os.path.join(
            Config._cache_dir,
            "fs-cache-%s-%d" % (service._my_coord.name,
                                service._my_coord.shard))
        self.tmp_dir = os.path.join(self.base_dir, "tmp")
        self.obj_dir = os.path.join(self.base_dir, "objects")
        if not mkdir(Config._cache_dir) or \
               not mkdir(self.base_dir) or \
               not mkdir(self.tmp_dir) or \
               not mkdir(self.obj_dir):
            logger.error("Cannot create necessary directories.")

    ## GET ##

    @make_async
    def get_file(self, digest, path=None, file_obj=None, string=None):
        """Get a file from the cache or from the service if not
        present.

        digest (string): the sha1 sum of the file.
        path (string): a path where to save the file.
        file_obj (file): a handler where to save the file.
        string (bool): True if forced to return content as a string.

        """

        cache_path = os.path.join(self.obj_dir, digest)
        cache_exists = os.path.exists(cache_path)
        data = None

        if cache_exists:
            # If there is the file in the cache, maybe it has been
            # deleted remotely. We need to ask.
            # TODO: since we never delete files, we could just give
            # the file without checking... and even if we delete file,
            # what's the problem?
            present = yield self.file_storage.is_file_present(digest=digest,
                                                              timeout=1)
            # File not available remotely, deleting from cache.
            if not present:
                try:
                    os.unlink(os.path.join(self.obj_dir, digest))
                except OSError:
                    pass
                yield async_error("IOError: 2 No such file or directory.")
                return

        else:
            data = yield self.file_storage.get_file(digest=digest,
                                                    timeout=True)
            try:
                with open(cache_path, "wb") as f:
                    f.write(data)
            except IOError as e:
                pass
            else:
                cache_exists = True

        # Here we have at least one amongst data not None
        # cache_exists.
        if data is None and not cache_exists:
            yield async_error("No data nor cache, this should not happen.")
            return

        # Saving to path
        if path is not None:
            if cache_exists:
                shutil.copy(cache_path, path)
            else: # data is not None
                try:
                    with open(path, "wb") as f:
                        f.write(data)
                except IOError as e:
                    yield async_error("Cannot save file %s to path "
                                      "`%s'. Error: %r" % (digest, path, e))
                    return

        # Saving to file object
        if file_obj is not None:
            if cache_exists:
                with open(cache_path, "rb") as f:
                    shutil.copyfileobj(f, file_obj)
            else: # data is not None
                file_obj.write(data)

        # Returning string?
        if string == True:
            if data is not None:
                yield async_response(data)
            else: # cache_exists
                data = open(cache_path, "rb").read()
            yield async_response(data)
        else:
            yield async_response(None)

    ## GET VARIATIONS ##

    def get_file_to_file(self, digest):
        """Get a file from the cache or from the service if not
        present. Returns it as a file-like object.

        digest (string): the sha1 sum of the file.
        return (file): an open handler for the file.

        """
        self.get_file(digest=digest)

    def get_file_to_write_file(self, digest, file_obj):
        """Get a file from the cache or from the service if not
        present. It writes it on a file-like object.

        digest (string): the sha1 sum of the file.
        file_obj (file): the file-like object on which to write
                         the received file.

        """
        self.get_file(digest=digest, file_obj=file_obj)

    def get_file_to_path(self, digest, path):
        """Get a file from the cache or from the service if not
        present. Returns it by putting it in the specified path.

        digest (string): the sha1 sum of the file.
        path (string): the path where to copy the received file.

        """
        self.get_file(digest=digest, path=path)

    def get_file_to_cache(self, digest):
        """Get a file from storage, but do not return it. Just keep it
        in the cache. Return True if file was successfully cached,
        False otherwise.

        digest (string): the sha1 sum of the file.

        """
        self.get_file(digest=digest)

    def get_file_to_string(self, digest):
        """Get a file from the cache or from the service if not
        present. Returns it as a string.

        digest (string): the sha1 sum of the file.
        return (string): the content of the file.

        """
        s = cStringIO.StringIO()
        self.get_file(digest=digest, file_obj=s)
        return s.getvalue()

    ## PUT ##

    @make_async
    def put_file(self, binary_data=None, description="",
                 file_obj=None, path=None):
        """Send a file to FileStorage, and keep a copy locally. The
        caller has to provide exactly one among binary_data, file_obj
        and path.

        binary_data (string): the content of the file to send.
        description (string): a human-readable description of the
                              content.
        file_obj (file): the file-like object to send.
        path (string): the file to send.

        """
        temp_path = os.path.join(self.tmp_dir, random_string(16))

        if [binary_data, file_obj, path].count(None) != 2:
            logger.error("No content (or too many) specified in put_file.")
            raise ValueError

        if path is not None:
            # If we cannot store locally the file, we do not report
            # errors.
            try:
                shutil.copy(path, temp_path)
            except IOError:
                pass
            # But if we cannot read the actual data, we are forced to
            # report.
            try:
                binary_data = open(path, "rb").read()
            except IOError as e:
                yield async_error(repr(e))
                return

        elif binary_data is not None:
            # Again, no error for inability of caching locally.
            try:
                open(temp_path, "wb").write(binary_data)
            except IOError:
                pass

        else: # file_obj is not None.
            binary_data = file_obj.read()
            try:
                open(temp_path, "wb").write(binary_data)
            except IOError:
                pass

        try:
            digest = yield self.file_storage.put_file(binary_data=binary_data,
                                                      description=description,
                                                      timeout=True)
            shutil.move(temp_path,
                        os.path.join(self.obj_dir, digest))
            yield async_response(digest)
        except Exception as e:
            yield async_error(repr(e))

    ## PUT SYNTACTIC SUGARS ##

    def put_file_from_string(self, content, description=""):
        """Send a file to FileStorage keeping a copy locally. The file
        is obtained from a string.

        This call is actually a syntactic sugar over put_file().

        content (string): the content of the file to send.
        description (string): a human-readable description of the
                              content.

        """
        self.put_file(binary_data=content, description=description)

    def put_file_from_file(self, file_obj, description=""):
        """Send a file to FileStorage keeping a copy locally. The file
        is obtained from a file-like object.

        This call is actually a syntactic sugar over put_file().

        file_obj (file): the file-like object to send.
        description (string): a human-readable description of the
                              content.

        """
        self.put_file(file_obj=file_obj, description=description)

    def put_file_from_path(self, path, description=""):
        """Send a file to FileStorage keeping a copy locally. The file is
        obtained from a file specified by its path.

        This call is actually a syntactic sugar over put_file().

        path (string): the file to send.
        description (string): ahuman-readable description of the
                              content.

        """
        self.put_file(path=path, description=description)

    ## OTHER ROUTINES ##

    @make_async
    def describe(self, digest):
        """Return the description of a file given its digest. This
        request is not actually cached, since is mostly meant for
        debugging purposes and it isn't used by the contest system
        itself.

        digest (string): the digest to describe.
        return (string): the description associated.

        """
        yield self.file_storage.describe(digest=digest)


def main():
    import sys
    if len(sys.argv) != 2:
        print sys.argv[0], "shard"
    else:
        FileStorage(shard=int(sys.argv[1])).run()


if __name__ == "__main__":
    main()
